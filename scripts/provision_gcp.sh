#!/usr/bin/env bash
# 一次性佈建 GCP 資源。全部 idempotent —— 重跑只會補上缺的東西。
#
# 依 docs/deploy-gcp.md 的順序執行，或直接 `bash scripts/provision_gcp.sh`。
# 每個階段都可以單獨跑：`bash scripts/provision_gcp.sh secret`
#
# 這支腳本**不會**寫入任何憑證值。secret 的內容由你手動放進去
# （見 `secret` 階段的提示），腳本只負責建立容器與授權。

set -euo pipefail

# Git Bash（MSYS）會把看起來像 Unix 路徑的參數改寫成 Windows 路徑。實測
# `mount-path=/app/state` 被改成 `mount-path=C:/Program Files/Git/app/state` ——
# 那是**容器內部**的路徑，不該被轉換，而且 gcloud 不會報錯，只會建出一台
# 掛載點錯誤的 VM。
#
# 只排除那一個參數，**不要**用 MSYS_NO_PATHCONV=1 或 MSYS2_ARG_CONV_EXCL='*'：
# Windows 版的 gcloud 是 bash 包裝 native python.exe，它需要靠這個轉換把自己的
# lib 路徑交給 python。全面關掉的話每一個 gcloud 呼叫都會死在
# 「can't open file 'C:\c\Users\...\gcloud.py'」。
export MSYS2_ARG_CONV_EXCL='--container-mount-disk='

PROJECT=ocard-ai
REGION=asia-east1
ZONE=asia-east1-b
INSTANCE=security-console
DISK=console-state
DISK_GB=20
# e2-medium（2 vCPU / 4 GB）而不是 e2-small：期間掃描以 6 條執行緒併發跑探針，
# 回傳的 rows 進 pandas；calibrate 還要算 28 天分布。2 GB 會在掃描長區間時 OOM，
# 而那是間歇性的、最難查的失敗。成本差約 US$14/月。
MACHINE=e2-medium
SA_NAME=security-console-vm
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SECRET=security-console-env
IMAGE=asia-east1-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/security-console/security-console
NETWORK_TAG=security-console
# 靜態**內網** IP。ocard-ros 的 rewrite 直接指這個位址，所以它不能漂移。
# 不能用內部 DNS（security-console.asia-east1-b.c.ocard-ai.internal）——
# Cloud Run 經 VPC connector 出去時的 DNS 解析不走 VPC 的內部 DNS 區域，
# `*.internal` 一律查不到。
INTERNAL_IP_NAME=security-console-internal
# ocard-ros 與 ocard-data-api 共用的 VPC connector 網段。ROS 的 rewrite 從這裡
# 打進主控台，所以防火牆只開放這一段。
CONNECTOR_CIDR=10.8.0.0/28
CONSOLE_PORT=8600

g() { gcloud --project="$PROJECT" "$@"; }
say() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
have() { "$@" >/dev/null 2>&1; }


# ── 1. Service account 與權限 ──────────────────────────────────────────
# 刻意**不**用預設的 compute service account：它在這個 project 上有
# roles/editor。主控台的 VM 只需要「讀一個 secret、拉映像、寫 log」。
stage_sa() {
  say "service account $SA_EMAIL"
  if have g iam service-accounts describe "$SA_EMAIL"; then
    echo "  已存在"
  else
    g iam service-accounts create "$SA_NAME" \
      --display-name="Security Log Console VM"
  fi

  for role in roles/logging.logWriter roles/monitoring.metricWriter \
              roles/artifactregistry.reader; do
    echo "  授予 $role"
    g projects add-iam-policy-binding "$PROJECT" \
      --member="serviceAccount:${SA_EMAIL}" --role="$role" \
      --condition=None --quiet >/dev/null
  done
}


# ── 2. Secret Manager ─────────────────────────────────────────────────
# 整份 .env 放成**一個** secret，而不是每個變數一個：
#   - core/config.py 的 load_dotenv() 本來就讀 .env 格式，應用程式零改動
#   - 輪換共用的 ClickHouse 密碼時只要改一個地方，不會漏掉
# 授權範圍限定在這一個 secret 上，不是 project 層級。
stage_secret() {
  say "secret $SECRET"
  if have g secrets describe "$SECRET"; then
    echo "  已存在（不覆寫；要更新請見下方指令）"
  else
    g secrets create "$SECRET" --replication-policy=automatic
    echo "  已建立，尚無版本"
  fi

  g secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role=roles/secretmanager.secretAccessor --quiet >/dev/null
  echo "  已授權 $SA_EMAIL 讀取"

  local versions
  versions=$(g secrets versions list "$SECRET" --filter='state:ENABLED' \
    --format='value(name)' 2>/dev/null | wc -l | tr -d ' ')
  if [ "$versions" = "0" ]; then
    cat <<'EOS'

  ⚠ 這個 secret 還沒有任何版本。內容是一份完整的 .env，需要這些鍵：

      CLICKHOUSE_HOST  CLICKHOUSE_PORT  CLICKHOUSE_USER  CLICKHOUSE_PASSWORD  CLICKHOUSE_DB
      MYSQL_HOST       MYSQL_PORT       MYSQL_USER       MYSQL_PASSWORD       MYSQL_DB
      FP_SECRET
      SLACK_WEBHOOK_URL
      ANTHROPIC_API_KEY
      ROS_BASE_URL=https://ros.ocard.co
      CONSOLE_BASE_URL=https://ros.ocard.co/security

  FP_SECRET 必須**固定不變** —— 它是 token 指紋的 HMAC 金鑰，改了以後
  同一個 API token 會算出不同指紋，Explorer 的 auth 維度與歷史事件對不起來。

  從本機 .env 建立第一版（記得先把 ROS_BASE_URL / CONSOLE_BASE_URL 改成正式值）：

      gcloud secrets versions add security-console-env --project=ocard-ai --data-file=.env

EOS
  else
    echo "  已有 $versions 個啟用中的版本"
  fi
}


# ── 3. 防火牆 ─────────────────────────────────────────────────────────
# VM 沒有外部 IP。對內只開兩個入口：ROS 的 VPC connector（服務流量）與
# IAP（維運 SSH）。ClickHouse／MySQL 是**出向**，走既有的 Cloud NAT，
# 不需要任何 ingress 規則。
stage_firewall() {
  say "防火牆規則"
  if have g compute firewall-rules describe "allow-ros-to-${NETWORK_TAG}"; then
    echo "  allow-ros-to-${NETWORK_TAG} 已存在"
  else
    g compute firewall-rules create "allow-ros-to-${NETWORK_TAG}" \
      --network=default --direction=INGRESS --action=ALLOW \
      --rules="tcp:${CONSOLE_PORT}" \
      --source-ranges="$CONNECTOR_CIDR" \
      --target-tags="$NETWORK_TAG" \
      --description="ocard-ros 的 VPC connector 打進主控台（/security rewrite）"
  fi

  if have g compute firewall-rules describe "allow-iap-ssh-${NETWORK_TAG}"; then
    echo "  allow-iap-ssh-${NETWORK_TAG} 已存在"
  else
    g compute firewall-rules create "allow-iap-ssh-${NETWORK_TAG}" \
      --network=default --direction=INGRESS --action=ALLOW --rules=tcp:22 \
      --source-ranges=35.235.240.0/20 \
      --target-tags="$NETWORK_TAG" \
      --description="IAP TCP forwarding：VM 沒有外部 IP，維運只能走 IAP"
  fi
}


# ── 4. 狀態磁碟 ───────────────────────────────────────────────────────
# 獨立於開機磁碟，且 auto-delete=no：裡面有 audit_log（誰調閱過哪一筆原文的
# 留痕）與 23 萬列 known_sources。VM 被誤刪時這些必須留下來。
stage_disk() {
  say "persistent disk $DISK"
  if have g compute disks describe "$DISK" --zone="$ZONE"; then
    echo "  已存在"
  else
    g compute disks create "$DISK" --zone="$ZONE" \
      --size="${DISK_GB}GB" --type=pd-balanced \
      --description="Security Log Console 狀態（SQLite monitor.db + logs）"
  fi

  say "每日快照排程"
  if have g compute resource-policies describe "${DISK}-daily" --region="$REGION"; then
    echo "  已存在"
  else
    g compute resource-policies create snapshot-schedule "${DISK}-daily" \
      --region="$REGION" --max-retention-days=14 \
      --daily-schedule --start-time=19:00 \
      --on-source-disk-delete=keep-auto-snapshots \
      --description="19:00 UTC = 03:00 台北，在 06:00 基線重算之前"
    g compute disks add-resource-policies "$DISK" --zone="$ZONE" \
      --resource-policies="${DISK}-daily"
  fi
}


# ── 5. 靜態內網 IP ────────────────────────────────────────────────────
stage_ip() {
  say "靜態內網 IP $INTERNAL_IP_NAME"
  if have g compute addresses describe "$INTERNAL_IP_NAME" --region="$REGION"; then
    echo -n "  已存在："
  else
    g compute addresses create "$INTERNAL_IP_NAME" \
      --region="$REGION" --subnet=default --purpose=GCE_ENDPOINT \
      --description="Security Log Console VM；ocard-ros 的 /security rewrite 指向它"
    echo -n "  已建立："
  fi
  g compute addresses describe "$INTERNAL_IP_NAME" --region="$REGION" \
    --format='value(address)'
}


# ── 6. VM ─────────────────────────────────────────────────────────────
# COS + 容器宣告（konlet）。磁碟由 konlet 以 --container-mount-disk 掛載，
# **不是**由 startup script 掛 —— konlet 自己掛就沒有「容器先起來、掛載後到」
# 的時序競賽。startup script 只負責「沒有檔案系統時格式化一次」。
#
# 磁碟還沒格式化時 konlet 掛載失敗 → 容器看不到哨兵檔 → entrypoint 以非零
# 退出 → restart policy 重啟。startup script 同時在格式化，所以會自己收斂。
stage_vm() {
  say "VM $INSTANCE"
  if have g compute instances describe "$INSTANCE" --zone="$ZONE"; then
    echo "  已存在。要換映像請用 CI，或："
    echo "    gcloud compute instances update-container $INSTANCE --zone=$ZONE --container-image=${IMAGE}:latest"
    return
  fi

  local internal_ip
  internal_ip=$(g compute addresses describe "$INTERNAL_IP_NAME" --region="$REGION" \
    --format='value(address)' 2>/dev/null || true)
  if [ -z "$internal_ip" ]; then
    echo "  ✗ 找不到保留的內網 IP。先跑：$0 ip" >&2
    return 1
  fi

  local startup
  startup=$(mktemp)
  cat > "$startup" <<'EOF'
#!/bin/bash
# 只做一件事：狀態磁碟沒有檔案系統時格式化它，並在磁碟上留下哨兵檔。
#
# 哨兵檔必須在**掛載後的磁碟上**建立，不能由 startup script 在
# /mnt/... 直接 touch —— 那樣磁碟沒掛好時會建在開機磁碟上，於是
# entrypoint 的檢查會通過，SQLite 靜靜寫到錯的地方。
set -euo pipefail
export PATH="/sbin:/usr/sbin:/bin:/usr/bin:$PATH"
DEV=/dev/disk/by-id/google-console-state

for _ in $(seq 1 30); do [ -b "$DEV" ] && break; sleep 2; done
if [ ! -b "$DEV" ]; then
  echo "console-state 磁碟未出現，放棄格式化" >&2
  exit 1
fi

if blkid "$DEV" >/dev/null 2>&1; then
  echo "console-state 已有檔案系統，不動它"
  exit 0
fi

echo "格式化 console-state（首次啟動）"
mkfs.ext4 -m 0 -F -L console-state "$DEV"
T=$(mktemp -d)
mount "$DEV" "$T"
mkdir -p "$T/logs"
: > "$T/.disk-ok"
umount "$T"
rmdir "$T"
echo "console-state 已就緒"
EOF

  # gcloud 需要能開啟這個檔案。在 Windows 上 gcloud 是原生 Python，
  # 讀不懂 MSYS 的 /tmp/... 路徑。
  local startup_arg="$startup"
  if command -v cygpath >/dev/null 2>&1; then
    startup_arg=$(cygpath -w "$startup")
  fi

  # create-with-container（不是 create）—— 容器宣告的旗標只有這個子命令認得。
  g compute instances create-with-container "$INSTANCE" \
    --zone="$ZONE" \
    --machine-type="$MACHINE" \
    --image-family=cos-stable --image-project=cos-cloud \
    --boot-disk-size=20GB --boot-disk-type=pd-balanced \
    --network=default --subnet=default \
    --no-address --private-network-ip="$INTERNAL_IP_NAME" \
    --tags="$NETWORK_TAG" \
    --service-account="$SA_EMAIL" \
    --scopes=https://www.googleapis.com/auth/cloud-platform \
    --disk="name=${DISK},device-name=${DISK},mode=rw,auto-delete=no" \
    --container-image="${IMAGE}:latest" \
    --container-restart-policy=always \
    --container-mount-disk="mount-path=/app/state,name=${DISK},mode=rw" \
    --container-env="CONSOLE_ENV_SECRET=projects/${PROJECT}/secrets/${SECRET}/versions/latest" \
    --metadata-from-file="startup-script=${startup_arg}" \
    --metadata="google-logging-enabled=true,google-monitoring-enabled=true" \
    --labels=app=security-console \
    --description="Ocard Security Log Console（掛在 ros.ocard.co/security）"

  rm -f "$startup"

  echo
  echo "  VM 已建立。ocard-ros 的 rewrite 要指向："
  echo "    http://${internal_ip}:${CONSOLE_PORT}"
}


# ── 7. push-to-main trigger ───────────────────────────────────────────
# 走 **2nd-gen** 的 GitHub 連線（`github-ocard`，asia-east1），不是 ocard-data-api
# 用的 1st-gen GitHub App trigger。1st-gen 需要在 GCP Console 完成一次互動式的
# repository 連結（`FAILED_PRECONDITION: Repository mapping does not exist`），
# 2nd-gen 則可以純指令建立映射，因為連線本身已經授權過整個 org。
stage_trigger() {
  say "Cloud Build repository 映射"
  if have g builds repositories describe ocard-security-console \
       --connection=github-ocard --region="$REGION"; then
    echo "  已存在"
  else
    g builds repositories create ocard-security-console \
      --connection=github-ocard --region="$REGION" \
      --remote-uri=https://github.com/Ocard-Inc/ocard-security-console.git
  fi

  say "Cloud Build trigger"
  if have g builds triggers describe security-console-main --region="$REGION"; then
    echo "  已存在"
    return
  fi
  g builds triggers create github \
    --name=security-console-main \
    --region="$REGION" \
    --repository="projects/${PROJECT}/locations/${REGION}/connections/github-ocard/repositories/ocard-security-console" \
    --branch-pattern='^main$' \
    --build-config=cloudbuild.yaml \
    --service-account="projects/${PROJECT}/serviceAccounts/732142852645-compute@developer.gserviceaccount.com" \
    --description='push 到 main 自動上版'
}


case "${1:-all}" in
  sa)       stage_sa ;;
  secret)   stage_secret ;;
  firewall) stage_firewall ;;
  disk)     stage_disk ;;
  ip)       stage_ip ;;
  vm)       stage_vm ;;
  trigger)  stage_trigger ;;
  # vm 需要映像已存在於 Artifact Registry，所以 all 不含它 —— 先跑一次 build
  # （見 docs/deploy-gcp.md 步驟 3），再跑 `provision_gcp.sh vm`。
  all)      stage_sa; stage_secret; stage_firewall; stage_disk; stage_ip ;;
  *) echo "用法：$0 [all|sa|secret|firewall|disk|ip|vm|trigger]" >&2; exit 2 ;;
esac

say "完成"

# 掛在 Ocard ROS 底下部署

主控台本身不做登入。身分一律由 **Ocard ROS**（統一登入入口）決定，權限來自
ROS 的動態 RBAC feature。本文說明如何把主控台掛到 `https://<ros-domain>/security`。

## 為什麼是「同網域子路徑」

ROS 用 NextAuth v5，session 存在**加密的 JWE cookie** 裡。cookie 預設是 host-only，
只有同網域才送得到。掛在同一網域的子路徑時，瀏覽器會自動把 ROS 的 session cookie
一併送給主控台，主控台再原樣轉發給 ROS 的 `/api/auth/me` 換取身分 —— 不必在
Python 端解密 NextAuth 的 cookie（那要複製 HKDF 細節，且會隨 NextAuth 改版而壞）。

跨網域部署則 cookie 不會共享，必須改走 OAuth redirect 流程，本文不涵蓋。

## 一、ROS 端：指派權限

`ocard-ros/lib/features.ts` 有一個 feature key，於 **設定 → 角色權限** 的
「資安監控」分組出現：

| Feature key | 效果 |
|---|---|
| `security.console` | 使用資安主控台的**完整功能** |

**沒有分級**：勾了就有全部功能（含唯讀 SQL、匯出、規則管理），沒勾就進不來。
主控台上顯示的「角色」是 ROS 那邊的角色名稱，不是主控台自己的等級。

沒有勾選的人登入後會看到「你尚未取得資安監控權限」，**不是**登入頁 —— 這兩種
狀況在畫面上刻意分開，否則使用者會一直重複登入卻進不來。

ROS 的 super-admin（`PROTECTED_ADMIN_EMAIL` 或 role=admin）自動擁有全部 feature，
不必額外指派。

## 二、主控台端：設定

`config/settings.yaml`：

```yaml
app:
  base_url: https://<ros-domain>/security   # Slack 告警連結的前綴

ros:
  base_url: https://<ros-domain>            # 留空 = 沒有任何登入保護
  enabled: true
  mount_path: /security                     # 登入完導回哪（本機直接跑時留空）
  cache_ttl_seconds: 30                     # 身分快取；設短一點讓權限撤銷即時生效
```

`ros.base_url` 一旦填入，任何請求都必須帶有效的 ROS session。
**正式環境務必填** —— 留空等於任何人都能直接進來。

`mount_path` 只用來組登入完的回跳網址。前端呼叫 API 的前綴不看設定，而是從
實際載入路徑推導（見 `web/index.html`），所以本機與 proxy 部署都不必改。

## 三、Reverse proxy

把 `/security` 開頭的請求轉給主控台，**保留路徑前綴**（不要 strip），並原樣
傳遞 cookie：

```nginx
location /security/ {
    proxy_pass http://127.0.0.1:8600/;   # 尾端斜線：剝掉 /security 再轉給後端
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_pass_request_headers on;        # cookie 必須帶過去
}
```

上面的寫法是「proxy 剝前綴、前端自己補前綴」：後端看到的是 `/api/session`，
瀏覽器送出的是 `/security/api/session`。兩邊對得上，因為前端從自己的載入路徑
（`/security/`）推導出前綴，補的正是 proxy 剝掉的那一段。

`/security/healthz` 不需登入，可作為存活檢查。

## 四、驗收

1. 用**沒有** `security.console` 的帳號開 `/security` → 應看到「你尚未取得
   資安監控權限」，並顯示登入中的 email（不是登入頁）。
2. 勾了 `security.console` 的帳號 → 左側八個項目都在（稽查模式目前隱藏，
   仍可用 `#/auditmode` 直接開），Log Explorer 與 SQL Console 都能開；
   左下角顯示 ROS 的真實姓名、email 與角色名。
3. 登出 ROS 後重整 → 應 302 到 ROS 登入頁，登入完自動回到 `/security`。
4. 在 ROS 把 `security.console` 取消 → 最多 30 秒後主控台就會擋下
   （`cache_ttl_seconds`），不必等 session 過期。
5. 把 ROS 停掉再開主控台 → 應顯示「無法驗證登入狀態」而不是登入頁，也不可放行。

## 常見狀況

**一直被導回登入頁**：多半是 cookie 沒送到。確認主控台與 ROS 真的在同一個網域
（不同 subdomain 也不行，除非 ROS 的 cookie domain 設成 `.ocard.co`），
且 proxy 有傳 cookie。

**登入後顯示無權限**：ROS 那邊的角色沒勾「資安監控」，或該使用者的 `active`
是 false。到 ROS 的 設定 → 角色權限 檢查。

**主控台顯示「無法驗證登入狀態」**：ROS 不可用或網路不通。此時主控台會拒絕所有
請求（回 503）而不是放行 —— 驗證不了身分就不該讓人看資安資料。

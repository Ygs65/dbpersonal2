# Cyberpunk MMORPG Frontend (MMORPG-grade UI)

這是一個「前端專案」：包含玩家端與 GM 控制台 UI。
後端 API 預設採用 `window.location.origin` 自動偵測（適合 Render 同站部署）。

## 檔案
- `index.html` 玩家端
- `admin.html` GM/後台（使用 `admin.js`）
- `app.js` 玩家端邏輯
- `style.css` Cyberpunk UI 主題（藍紫雲霧）
- `assets/` SVG 分隔線與光條
- `delete_data.py` Redis 清資料工具（升級版）

## 後端 API 端點（前端會呼叫）
玩家端常用：
- `POST /auth/register`
- `POST /auth/login`
- `GET  /player/stats`
- `POST /player/exp`
- `POST /equip/wear`
- `POST /equip/unwear`
- `POST /equip/enhance`
- `POST /battle/pvp`
- `GET  /rank/power` / `GET /rank/elo` / `GET /rank/weekly`
- `GET  /shop/list`
- `POST /shop/buy`
- `GET  /shop/inventory`
- `GET  /auction/list`
- `POST /auction/create`
- `POST /auction/bid`
- `POST /auction/buy`
- `POST /friend/request`
- `GET  /friend/requests`
- `POST /friend/accept`
- `POST /friend/reject`
- `POST /friend/remove`
- `GET  /friend/list`

GM 控制台會呼叫：
- `POST /admin/login`
- `GET  /admin/players`
- `POST /admin/ban/<user>`
- `POST /admin/unban/<user>`
- `POST /admin/unlock/<user>`
- `POST /admin/announce/add`
- `GET  /admin/announce/list`
- `DELETE /admin/announce/delete/<idx>`
- `POST /admin/announce/clear`
- `GET  /admin/logs`
- `GET  /admin/battles`
- `GET  /admin/auction/sold`

## SocketIO（可選）
如果你的後端同站提供 Socket.IO，前端會嘗試連線並接收：
- `announce`
- `auction_sold`
- `friend_online`

## 部署
任意靜態網站都可（Render Static Site / GitHub Pages / Cloudflare Pages）。
若要同站 API，建議：Render Web Service（後端）+ 靜態前端由後端提供（或同站）。


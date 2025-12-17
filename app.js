// ===============================
// Global State
// ===============================
let currentUser = null;      // string
let authToken = null;        // string (player session token)
let playerInfo = null;       // 最新玩家資料
let shopCache = null;        // 商店道具快取

// ===============================
// DOM Helpers
// ===============================
function $(id) {
    return document.getElementById(id);
}

function setText(id, value) {
    const el = $(id);
    if (el) el.textContent = value;
}

function showMessage(id, msg, isError = true) {
    const el = $(id);
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = isError ? "#ff99bb" : "#9fffb2";
}

// ===============================
// API Helper
// ===============================
async function apiRequest(path, method = "GET", body = null, useAuth = false) {
    const headers = {
        "Content-Type": "application/json"
    };
    if (useAuth && authToken) {
        headers["Authorization"] = "Bearer " + authToken;
    }

    const options = { method, headers };
    if (body) {
        options.body = JSON.stringify(body);
    }

    const res = await fetch(path, options);
    let data = null;
    try {
        data = await res.json();
    } catch (e) {
        // ignore
    }

    return { status: res.status, ok: res.ok, data };
}

// ===============================
// Page Switching
// ===============================
function showPage(pageId) {
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    const page = $(pageId);
    if (page) page.classList.add("active");
}

function openSection(name) {
    document.querySelectorAll(".panel-section").forEach(sec => sec.classList.remove("active"));
    const sec = $("section-" + name);
    if (sec) sec.classList.add("active");

    // 自動載入資料
    if (name === "shop") {
        loadShopItems();
    } else if (name === "inventory") {
        loadInventory();
    } else if (name === "auction") {
        loadAuctionList();
    }
}

// ===============================
// Auth: Register / Login / Logout
// ===============================
async function registerUser() {
    const username = $("auth-username").value.trim();
    const password = $("auth-password").value;
    const password2 = $("auth-password2").value;

    showMessage("auth-message", "");

    if (!username || !password || !password2) {
        showMessage("auth-message", "請填寫所有欄位");
        return;
    }

    const body = {
        username,
        password,
        confirm_password: password2
    };

    try {
        const res = await apiRequest("/auth/register", "POST", body, false);
        if (!res.ok || !res.data || !res.data.success) {
            const msg = res.data && res.data.message ? res.data.message : "註冊失敗";
            showMessage("auth-message", msg);
            return;
        }

        showMessage("auth-message", "註冊成功，請直接登入", false);
    } catch (e) {
        console.error(e);
        showMessage("auth-message", "無法連線伺服器");
    }
}

async function loginUser() {
    const username = $("auth-username").value.trim();
    const password = $("auth-password").value;

    showMessage("auth-message", "");

    if (!username || !password) {
        showMessage("auth-message", "請輸入帳號與密碼");
        return;
    }

    const body = {
        username,
        password,
        device: navigator.userAgent || ""
    };

    try {
        const res = await apiRequest("/auth/login", "POST", body, false);

        // 特別處理 HTTP 狀態與 code
        if (!res.ok) {
            if (res.data && res.data.code === "ALREADY_LOGGED_IN") {
                const info = res.data;
                let msg = "此帳號已在其他裝置登入\n";
                msg += `裝置: ${info.device || "-"}\n`;
                msg += `IP: ${info.ip || "-"}\n`;
                msg += `登入時間: ${info.login_time || "-"}`;
                showMessage("auth-message", msg.replace(/\n/g, "；"));
                return;
            }

            const msg = (res.data && res.data.message) ? res.data.message : "登入失敗";
            showMessage("auth-message", msg);
            return;
        }

        const data = res.data;
        if (!data || !data.success) {
            showMessage("auth-message", (data && data.message) || "登入失敗");
            return;
        }

        // 登入成功
        authToken = data.token;
        currentUser = data.username;
        playerInfo = data.player || null;

        updatePlayerUI();
        showPage("page-dashboard");
        openSection("click");

        // 清空密碼欄位
        $("auth-password").value = "";
        $("auth-password2").value = "";
        showMessage("auth-message", "");
    } catch (e) {
        console.error(e);
        showMessage("auth-message", "無法連線伺服器");
    }
}

async function logoutUser() {
    if (!currentUser || !authToken) {
        // 直接回登入頁
        currentUser = null;
        authToken = null;
        playerInfo = null;
        showPage("page-auth");
        return;
    }

    const body = {
        username: currentUser,
        token: authToken
    };

    try {
        await apiRequest("/auth/logout", "POST", body, false);
    } catch (e) {
        console.error(e);
    }

    currentUser = null;
    authToken = null;
    playerInfo = null;
    showPage("page-auth");
}

// ===============================
// Player Info UI
// ===============================
function updatePlayerUI() {
    if (!playerInfo) {
        setText("player-username", currentUser || "-");
        setText("player-gold", "0");
        setText("player-level", "1");
        setText("player-exp", "0");
        return;
    }
    setText("player-username", playerInfo.username || currentUser || "-");
    setText("player-gold", playerInfo.gold != null ? playerInfo.gold : "0");
    setText("player-level", playerInfo.level != null ? playerInfo.level : "1");
    setText("player-exp", playerInfo.exp != null ? playerInfo.exp : "0");
}

async function refreshPlayerInfo() {
    if (!currentUser) return;
    try {
        const res = await apiRequest(`/player/${encodeURIComponent(currentUser)}`, "GET", null, false);
        if (res.ok && res.data && res.data.success && res.data.player) {
            playerInfo = res.data.player;
            updatePlayerUI();
        }
    } catch (e) {
        console.error(e);
    }
}

// ===============================
// Click Gold
// ===============================
let clickCooldown = false;
let clickCooldownTimer = null;

async function clickGold() {
    if (!currentUser || !authToken) {
        alert("請先登入");
        return;
    }
    if (clickCooldown) {
        // 忽略，或可顯示提示
        return;
    }

    const btn = $("click-btn");
    btn.disabled = true;

    try {
        const res = await apiRequest(`/click/${encodeURIComponent(currentUser)}`, "POST", null, true);

        if (res.status === 429) {
            // 被限流
            const d = res.data || {};
            const retryMs = d.retry_after_ms != null ? d.retry_after_ms : (d.cooldown_ms || 1000);
            startClickCooldown(retryMs);
            btn.disabled = false;
            return;
        }

        if (!res.ok || !res.data || !res.data.success) {
            btn.disabled = false;
            alert((res.data && res.data.message) || "點擊失敗");
            return;
        }

        const d = res.data;
        // 更新 UI
        setText("player-gold", d.gold);
        if (playerInfo) {
            playerInfo.gold = d.gold;
        }

        setText("combo-value", d.combo);
        setText("critical-value", d.critical ? "YES!" : "-");
        setText("total-clicks-value", d.total_clicks);

        // 特效
        btn.classList.add("combo-glow");
        setTimeout(() => btn.classList.remove("combo-glow"), 600);

        // 啟動冷卻
        const cooldownMs = d.cooldown_ms || 500;
        startClickCooldown(cooldownMs);

        btn.disabled = false;
    } catch (e) {
        console.error(e);
        btn.disabled = false;
        alert("無法連線伺服器");
    }
}

function startClickCooldown(ms) {
    clickCooldown = true;
    if (clickCooldownTimer) {
        clearTimeout(clickCooldownTimer);
    }
    clickCooldownTimer = setTimeout(() => {
        clickCooldown = false;
    }, ms);
}

// ===============================
// Shop
// ===============================
async function loadShopItems() {
    if (!currentUser || !authToken) {
        alert("請先登入");
        return;
    }

    // 後端 shop/items 不需要登入，但這裡登入後才開啟功能
    try {
        const res = await apiRequest("/shop/items", "GET", null, false);
        if (!res.ok || !res.data) {
            alert("無法載入商店資料");
            return;
        }
        shopCache = res.data.items || [];
        renderShopItems(shopCache);
    } catch (e) {
        console.error(e);
        alert("無法載入商店資料");
    }
}

function renderShopItems(items) {
    const container = $("shop-items");
    container.innerHTML = "";

    if (!items || items.length === 0) {
        container.innerHTML = `<div class="list-card">目前沒有可購買的道具</div>`;
        return;
    }

    for (const item of items) {
        const card = document.createElement("div");
        card.className = "list-card";

        card.innerHTML = `
            <div class="list-card-title">${item.name} (${item.item_id})</div>
            <div class="list-card-meta">價格：${item.price} 金幣</div>
            <div class="list-card-desc">${item.desc || ""}</div>
            <div class="btn-row">
                <button class="cyber-btn" onclick="openBuyItemModal('${item.item_id}')">購買</button>
            </div>
        `;

        container.appendChild(card);
    }
}

function openBuyItemModal(itemId) {
    if (!shopCache) return;
    const item = shopCache.find(i => i.item_id === itemId);
    if (!item) return;

    const html = `
        <h3 class="cyber-title">購買道具</h3>
        <p style="font-size:13px; margin-bottom:6px;">
            道具：<strong>${item.name}</strong> (${item.item_id})<br>
            價格：${item.price} 金幣 / 個<br>
            堆疊上限：${item.max_stack}
        </p>
        <div class="input-group">
            <label>購買數量</label>
            <input id="buy-qty" type="number" min="1" value="1" />
        </div>
        <div class="btn-row">
            <button class="cyber-btn" onclick="confirmBuyItem('${item.item_id}')">確認購買</button>
        </div>
        <div id="modal-msg" class="message"></div>
    `;
    openModal(html);
}

async function confirmBuyItem(itemId) {
    const qtyEl = $("buy-qty");
    const qty = parseInt(qtyEl.value, 10) || 0;

    if (qty <= 0) {
        showModalMsg("數量需大於 0");
        return;
    }

    if (!currentUser || !authToken) {
        showModalMsg("請先登入");
        return;
    }

    const body = {
        username: currentUser,
        item_id: itemId,
        qty
    };

    try {
        const res = await apiRequest("/shop/buy", "POST", body, true);
        if (!res.ok || !res.data || !res.data.success) {
            const d = res.data || {};
            showModalMsg(d.message || "購買失敗");
            return;
        }

        const d = res.data;
        // 更新金幣
        if (playerInfo) {
            playerInfo.gold = d.gold;
        }
        setText("player-gold", d.gold);

        showModalMsg("購買成功！", false);
        // 可順便刷新背包（若現在在背包頁）
        if (document.getElementById("section-inventory").classList.contains("active")) {
            loadInventory();
        }
    } catch (e) {
        console.error(e);
        showModalMsg("無法連線伺服器");
    }
}

// ===============================
// Inventory
// ===============================
async function loadInventory() {
    if (!currentUser || !authToken) {
        alert("請先登入");
        return;
    }

    try {
        const res = await apiRequest(`/shop/inventory/${encodeURIComponent(currentUser)}`, "GET", null, true);
        if (!res.ok || !res.data || !res.data.success) {
            alert((res.data && res.data.message) || "無法載入背包");
            return;
        }

        const inv = res.data.inventory || { items: [] };
        renderInventory(inv.items || []);
    } catch (e) {
        console.error(e);
        alert("無法載入背包");
    }
}

function renderInventory(items) {
    const container = $("inventory-items");
    container.innerHTML = "";

    if (!items || items.length === 0) {
        container.innerHTML = `<div class="list-card">背包目前是空的</div>`;
        return;
    }

    for (const item of items) {
        const card = document.createElement("div");
        card.className = "list-card";
        card.innerHTML = `
            <div class="list-card-title">${item.item_id}</div>
            <div class="list-card-meta">數量：${item.qty}</div>
        `;
        container.appendChild(card);
    }
}

// ===============================
// Auction
// ===============================
async function loadAuctionList() {
    try {
        const res = await apiRequest("/auction/list?limit=50", "GET", null, false);
        if (!res.ok || !res.data) {
            alert("無法載入拍賣列表");
            return;
        }

        const list = res.data.items || [];
        renderAuctionList(list);
    } catch (e) {
        console.error(e);
        alert("無法載入拍賣列表");
    }
}

function renderAuctionList(items) {
    const container = $("auction-list");
    container.innerHTML = "";

    if (!items || items.length === 0) {
        container.innerHTML = `<div class="list-card">目前沒有開放中的拍賣</div>`;
        return;
    }

    for (const a of items) {
        const card = document.createElement("div");
        card.className = "list-card";

        const isMine = currentUser && a.seller === currentUser;
        const buyoutText = a.buyout_price ? `${a.buyout_price}` : "未設定";
        const statusText = a.status || "open";

        let buttonsHtml = `
            <button class="cyber-btn" onclick="openAuctionDetail(${a.auction_id})">詳情 / 出價</button>
        `;

        if (isMine && statusText === "open" && !a.current_bidder) {
            buttonsHtml += `
                <button class="cyber-btn danger-btn" onclick="cancelAuction(${a.auction_id})">取消拍賣</button>
            `;
        }

        card.innerHTML = `
            <div class="list-card-title">#${a.auction_id} - ${a.item_id}</div>
            <div class="list-card-meta">
                賣家：${a.seller}<br>
                數量：${a.qty}<br>
                起標價：${a.start_price}<br>
                目前價：${a.current_price}（出價者：${a.current_bidder || "無"}）<br>
                直購價：${buyoutText}<br>
                狀態：${statusText}
            </div>
            <div class="btn-row">
                ${buttonsHtml}
            </div>
        `;
        container.appendChild(card);
    }
}

async function openAuctionDetail(auctionId) {
    try {
        const res = await apiRequest(`/auction/${auctionId}`, "GET", null, false);
        if (!res.ok || !res.data || !res.data.success) {
            alert("無法取得拍賣詳細資訊");
            return;
        }

        const a = res.data.auction;
        const buyoutText = a.buyout_price ? `${a.buyout_price}` : "未設定";

        let actionButtons = "";
        if (currentUser && a.status === "open" && a.seller !== currentUser) {
            actionButtons = `
                <div class="input-group">
                    <label>出價金額（需大於目前價格）</label>
                    <input id="bid-amount" type="number" min="${a.current_price + 1}" value="${a.current_price + 1}" />
                </div>
                <div class="btn-row">
                    <button class="cyber-btn" onclick="placeBid(${a.auction_id})">出價</button>
                    <button class="cyber-btn" onclick="buyNow(${a.auction_id})">直購</button>
                </div>
            `;
        } else if (currentUser && a.seller === currentUser && a.status === "open" && !a.current_bidder) {
            actionButtons = `
                <div class="btn-row">
                    <button class="cyber-btn danger-btn" onclick="cancelAuction(${a.auction_id})">取消拍賣</button>
                </div>
            `;
        }

        const html = `
            <h3 class="cyber-title">拍賣詳情 #${a.auction_id}</h3>
            <p style="font-size:13px;margin-bottom:6px;">
                賣家：${a.seller}<br>
                物品：${a.item_id}<br>
                數量：${a.qty}<br>
                起標價：${a.start_price}<br>
                目前價格：${a.current_price}（出價者：${a.current_bidder || "無"}）<br>
                直購價：${buyoutText}<br>
                狀態：${a.status}<br>
                建立時間：${a.created_at}
            </p>
            ${actionButtons}
            <div id="modal-msg" class="message"></div>
        `;
        openModal(html);
    } catch (e) {
        console.error(e);
        alert("無法取得拍賣詳細資訊");
    }
}

async function placeBid(auctionId) {
    if (!currentUser || !authToken) {
        showModalMsg("請先登入");
        return;
    }
    const input = $("bid-amount");
    if (!input) {
        showModalMsg("找不到出價輸入欄位");
        return;
    }
    const bid = parseInt(input.value, 10) || 0;
    if (bid <= 0) {
        showModalMsg("出價金額需大於 0");
        return;
    }

    const body = {
        username: currentUser,
        auction_id: auctionId,
        bid_amount: bid
    };

    try {
        const res = await apiRequest("/auction/bid", "POST", body, true);
        if (!res.ok || !res.data || !res.data.success) {
            const d = res.data || {};
            showModalMsg(d.message || "出價失敗");
            return;
        }

        showModalMsg("出價成功！", false);
        loadAuctionList();
    } catch (e) {
        console.error(e);
        showModalMsg("無法連線伺服器");
    }
}

async function buyNow(auctionId) {
    if (!currentUser || !authToken) {
        showModalMsg("請先登入");
        return;
    }

    const body = {
        username: currentUser,
        auction_id: auctionId
    };

    try {
        const res = await apiRequest("/auction/buy_now", "POST", body, true);
        if (!res.ok || !res.data || !res.data.success) {
            const d = res.data || {};
            showModalMsg(d.message || "直購失敗");
            return;
        }

        const d = res.data;
        if (playerInfo) {
            playerInfo.gold = d.buyer_gold_after;
        }
        setText("player-gold", d.buyer_gold_after);

        showModalMsg("直購成功！", false);
        loadAuctionList();
        loadInventory();
    } catch (e) {
        console.error(e);
        showModalMsg("無法連線伺服器");
    }
}

async function cancelAuction(auctionId) {
    if (!currentUser || !authToken) {
        alert("請先登入");
        return;
    }

    const sure = confirm(`確定要取消拍賣 #${auctionId} 嗎？\n（僅在尚未有人出價時可以取消）`);
    if (!sure) return;

    try {
        const res = await fetch(`/auction/cancel/${auctionId}`, {
            method: "POST",
            headers: {
                "Authorization": "Bearer " + authToken,
                "X-Username": currentUser
            }
        });
        const data = await res.json().catch(() => null);

        if (!res.ok || !data || !data.success) {
            alert((data && data.message) || "取消失敗");
            return;
        }

        alert("拍賣已取消，物品已退回背包");
        closeModal();
        loadAuctionList();
        loadInventory();
    } catch (e) {
        console.error(e);
        alert("無法連線伺服器");
    }
}

function openAuctionCreate() {
    if (!currentUser || !authToken) {
        alert("請先登入");
        return;
    }
    // 先載入背包，列出可上架道具
    loadInventoryForAuctionCreate();
}

async function loadInventoryForAuctionCreate() {
    try {
        const res = await apiRequest(`/shop/inventory/${encodeURIComponent(currentUser)}`, "GET", null, true);
        if (!res.ok || !res.data || !res.data.success) {
            alert("無法載入背包");
            return;
        }
        const inv = res.data.inventory || { items: [] };
        const items = inv.items || [];
        if (!items.length) {
            alert("背包沒有可以上架的道具");
            return;
        }

        let optionsHtml = "";
        for (const it of items) {
            optionsHtml += `<option value="${it.item_id}" data-max="${it.qty}">${it.item_id}（數量：${it.qty}）</option>`;
        }

        const html = `
            <h3 class="cyber-title">上架拍賣</h3>
            <div class="input-group">
                <label>選擇道具</label>
                <select id="auction-item-id">${optionsHtml}</select>
            </div>
            <div class="input-group">
                <label>上架數量</label>
                <input id="auction-qty" type="number" min="1" value="1" />
            </div>
            <div class="input-group">
                <label>起標價</label>
                <input id="auction-start-price" type="number" min="1" value="10" />
            </div>
            <div class="input-group">
                <label>直購價（可留空）</label>
                <input id="auction-buyout-price" type="number" min="0" value="" />
            </div>
            <div class="btn-row">
                <button class="cyber-btn" onclick="confirmCreateAuction()">確認上架</button>
            </div>
            <div id="modal-msg" class="message"></div>
        `;
        openModal(html);
    } catch (e) {
        console.error(e);
        alert("無法載入背包");
    }
}

async function confirmCreateAuction() {
    const itemSelect = $("auction-item-id");
    const qtyInput = $("auction-qty");
    const startInput = $("auction-start-price");
    const buyoutInput = $("auction-buyout-price");

    if (!itemSelect || !qtyInput || !startInput) {
        showModalMsg("資料不完整");
        return;
    }

    const itemId = itemSelect.value;
    const qty = parseInt(qtyInput.value, 10) || 0;
    const startPrice = parseInt(startInput.value, 10) || 0;
    const buyout = buyoutInput.value ? parseInt(buyoutInput.value, 10) : null;

    if (!itemId || qty <= 0 || startPrice <= 0) {
        showModalMsg("請檢查數量與起標價");
        return;
    }

    // 檢查是否超過背包數量（利用 data-max）
    const selectedOption = itemSelect.selectedOptions[0];
    const maxQty = parseInt(selectedOption.getAttribute("data-max") || "0", 10);
    if (qty > maxQty) {
        showModalMsg(`上架數量不可超過擁有數量（目前 ${maxQty}）`);
        return;
    }

    if (!currentUser || !authToken) {
        showModalMsg("請先登入");
        return;
    }

    const body = {
        username: currentUser,
        item_id: itemId,
        qty,
        start_price: startPrice,
        buyout_price: buyout || undefined
    };

    try {
        const res = await apiRequest("/auction/create", "POST", body, true);
        if (!res.ok || !res.data || !res.data.success) {
            const d = res.data || {};
            showModalMsg(d.message || "上架失敗");
            return;
        }

        showModalMsg("上架成功！", false);
        loadAuctionList();
        loadInventory();
    } catch (e) {
        console.error(e);
        showModalMsg("無法連線伺服器");
    }
}

// ===============================
// Modal
// ===============================
function openModal(html) {
    const modal = $("modal");
    const body = $("modal-body");
    if (body) body.innerHTML = html || "";
    if (modal) modal.classList.add("show");
}

function closeModal() {
    const modal = $("modal");
    if (modal) modal.classList.remove("show");
}

function showModalMsg(msg, isError = true) {
    const el = $("modal-msg");
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = isError ? "#ff99bb" : "#9fffb2";
}

// ===============================
// Init
// ===============================
window.addEventListener("DOMContentLoaded", () => {
    // 預設顯示登入頁
    showPage("page-auth");
});

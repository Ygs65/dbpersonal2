/* Cyberpunk MMORPG Frontend (Render auto-detect API base) */
const API_BASE = window.location.origin;
let TOKEN = null;
let USERNAME = null;

const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));

function toast(msg, kind='info'){
  const el = $('#toast');
  if(!el) return;
  el.textContent = msg;
  el.className = 'toast toast--show toast--' + kind;
  setTimeout(()=>{ el.className='toast'; }, 2200);
}

async function api(path, {method='GET', body=null, auth=true} = {}){
  const headers = {'Content-Type':'application/json'};
  if(auth && TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
  const res = await fetch(API_BASE + path, {
    method, headers,
    body: body ? JSON.stringify(body) : null
  });
  let data = null;
  try{ data = await res.json(); }catch(_){ data = {success:false, message:'非 JSON 回應'}; }
  if(!res.ok && data && !data.success && !data.message) data.message = 'HTTP ' + res.status;
  return {ok: res.ok, data};
}

function setMiniTab(name){
  $$('.tabs-mini__btn').forEach(b=>b.classList.toggle('tabs-mini__btn--active', b.dataset.mini===name));
  $$('#screen-login .form').forEach(f=>f.classList.toggle('form--active', f.id === (name==='login' ? 'form-login' : 'form-register')));
}

function startTransition(){
  const t = $('#login-transition');
  t.classList.add('transition--on');
  // 2.5s per user choice (4B)
  setTimeout(()=>t.classList.remove('transition--on'), 2500);
}

function setPage(page){
  $$('.page').forEach(p=>p.classList.toggle('page--active', p.id==='page-'+page));
  $$('.bnav__btn').forEach(b=>b.classList.toggle('bnav__btn--active', b.dataset.page===page));
}

async function login(){
  const u = $('#login-username').value.trim();
  const p = $('#login-password').value;
  $('#login-msg').textContent = '';
  if(!u || !p){ $('#login-msg').textContent='請輸入帳號密碼'; return; }

  const {data} = await api('/auth/login', {method:'POST', body:{username:u, password:p, device:'web'}, auth:false});
  if(!data.success){
    $('#login-msg').textContent = data.message || '登入失敗';
    if(data.code==='ALREADY_LOGGED_IN') toast('帳號已在其他裝置登入', 'warn');
    return;
  }
  TOKEN = data.token;
  USERNAME = data.username || u;

  startTransition();
  setTimeout(async ()=>{
    $('#screen-login').classList.remove('screen--active');
    $('#screen-game').classList.add('screen--active');
    toast('登入成功', 'ok');
    setPage('profile');
    await refreshAll();
  }, 2550);
}

async function register(){
  const u = $('#reg-username').value.trim();
  const p1 = $('#reg-password').value;
  const p2 = $('#reg-password2').value;
  $('#register-msg').textContent = '';
  if(!u || !p1 || !p2){ $('#register-msg').textContent='請填寫完整'; return; }
  if(p1!==p2){ $('#register-msg').textContent='兩次密碼不一致'; return; }

  const {data} = await api('/auth/register', {method:'POST', body:{username:u, password:p1, confirm_password:p2}, auth:false});
  if(!data.success){ $('#register-msg').textContent=data.message||'註冊失敗'; return; }
  toast('註冊成功，請登入', 'ok');
  setMiniTab('login');
}

async function logout(){
  TOKEN=null; USERNAME=null;
  $('#screen-game').classList.remove('screen--active');
  $('#screen-login').classList.add('screen--active');
  toast('已登出', 'info');
}

function fmt(n){ return (n===null||n===undefined) ? '—' : String(n); }
function clamp01(x){ return Math.max(0, Math.min(1, x)); }

// ---------------- Profile ----------------
async function loadStats(){
  const {data} = await api('/player/stats');
  if(!data.success){ toast(data.message||'讀取失敗','warn'); return null; }
  $('#who-name').textContent = USERNAME || '—';
  $('#who-power').textContent = 'Power ' + fmt(data.power);
  $('#stats-json').textContent = JSON.stringify(data.stats, null, 2);
  return data;
}

async function loadMe(){
  // user meta via stats + optional user fields endpoints
  // If you have /player/me, wire it here. For now keep minimal.
  const stats = await loadStats();
  if(!stats) return;
  // Try to read gold/level/exp if backend provides /player/me (optional)
  const me = await api('/player/me');
  if(me.ok && me.data && me.data.success){
    $('#who-gold').textContent = fmt(me.data.gold);
    $('#who-level').textContent = 'LV ' + fmt(me.data.level);
    $('#p-level').textContent = fmt(me.data.level);
    $('#p-exp').textContent = fmt(me.data.exp);
    // If backend gives exp_required, we can set bar. Otherwise infer 0..1 as placeholder.
    const ratio = me.data.exp_required ? (me.data.exp / me.data.exp_required) : 0.25;
    $('#exp-bar').style.width = (clamp01(ratio)*100).toFixed(0)+'%';
  }else{
    // fallback display
    $('#who-gold').textContent = '—';
    $('#who-level').textContent = 'LV —';
  }
}

async function addExp(){
  const v = parseInt($('#exp-add').value || '0', 10);
  if(!v){ $('#exp-msg').textContent='請輸入數字'; return; }
  $('#exp-msg').textContent='';
  const {data} = await api('/player/exp', {method:'POST', body:{exp:v}});
  if(!data.success){ $('#exp-msg').textContent=data.message||'失敗'; return; }
  toast('已送出 EXP', 'ok');
  $('#exp-add').value='';
  await refreshAll();
}

// ---------------- Equip ----------------
let selectedEquipUid = null;
let equipCache = [];

function renderSlots(){
  const slots = ['weapon','head','body','hands','feet'];
  const el = $('#equip-slots');
  el.innerHTML = '';
  slots.forEach(s=>{
    const div=document.createElement('div');
    div.className='slot';
    div.innerHTML = `<div class="slot__name">${s.toUpperCase()}</div><div class="slot__uid mono" id="slot-${s}">—</div>
      <button class="btn btn--ghost slot__btn" data-slot="${s}" data-act="unwear">卸下</button>`;
    el.appendChild(div);
  });
}

function equipCard(eq){
  const uid = eq.uid;
  const rarity = (eq.rarity||'').toUpperCase();
  const enh = eq.enhance ?? 0;
  const attrs = ['atk','def','hp','spd','crit','crit_dmg'].map(k=>`${k}:${eq[k]}`).join('  ');
  return `
  <div class="item ${selectedEquipUid===uid?'item--active':''}" data-uid="${uid}">
    <div class="item__top">
      <div class="badge badge--rarity">${rarity}</div>
      <div class="mono item__uid">${uid}</div>
    </div>
    <div class="item__mid">
      <div class="item__name">${eq.equip_type||'equip'} <span class="badge badge--enh">+${enh}</span></div>
      <div class="item__attrs mono">${attrs}</div>
    </div>
    <div class="item__actions">
      <button class="btn btn--ghost" data-act="wear" data-slot="${eq.equip_type||'weapon'}">穿戴</button>
      <button class="btn btn--ghost" data-act="select">選中</button>
    </div>
  </div>`;
}

function renderEquipList(){
  const q = ($('#equip-search').value || '').trim().toLowerCase();
  const list = $('#equip-list');
  const items = equipCache.filter(e=>{
    if(!q) return true;
    return JSON.stringify(e).toLowerCase().includes(q);
  });
  list.innerHTML = items.length ? items.map(e=>equipCard(e)).join('') : `<div class="empty">無裝備資料（可用「測試掉落」產生）</div>`;
}

async function loadEquips(){
  // backend should provide /player/equips ideally; if not, you can map to admin endpoint or custom endpoint.
  // We attempt /player/equips first; fallback to /admin/equip/<me> if enabled.
  let data = (await api('/player/equips')).data;
  if(!data || !data.success){
    const alt = await api('/admin/equip/'+encodeURIComponent(USERNAME));
    if(alt.ok && alt.data && alt.data.success) data = {success:true, equips: alt.data.equips};
  }
  if(!data || !data.success){
    equipCache = [];
    renderEquipList();
    return;
  }
  equipCache = (data.equips||[]).map(e=>({ ...e }));
  renderEquipList();
}

function setSelected(uid){
  selectedEquipUid = uid;
  const eq = equipCache.find(x=>x.uid===uid);
  $('#enh-uid').textContent = uid || '—';
  $('#enh-level').textContent = eq ? ('+'+(eq.enhance??0)) : '—';
  renderEquipList();
}

async function wearEquip(uid, slot){
  const {data} = await api('/equip/wear', {method:'POST', body:{uid, slot}});
  if(!data.success){ toast(data.message||'失敗','warn'); return; }
  toast('已穿戴', 'ok');
  await refreshAll();
}
async function unwearSlot(slot){
  const {data} = await api('/equip/unwear', {method:'POST', body:{slot}});
  if(!data.success){ toast(data.message||'失敗','warn'); return; }
  toast('已卸下', 'ok');
  await refreshAll();
}
async function enhance(){
  if(!selectedEquipUid){ $('#enh-msg').textContent='請先選中一件裝備'; return; }
  $('#enh-msg').textContent='';
  const use_guard = $('#enh-guard').checked;
  const {data} = await api('/equip/enhance', {method:'POST', body:{uid:selectedEquipUid, use_guard}});
  if(data.success){
    fx('enh-fx','SUCCESS');
    toast('強化成功 +'+data.enhance, 'ok');
  }else{
    if(data.explode){
      fx('enh-fx','BOOM');
      toast('爆炸！裝備消失', 'warn');
      selectedEquipUid=null;
      $('#enh-uid').textContent='—';
      $('#enh-level').textContent='—';
    }else{
      fx('enh-fx','FAIL');
      toast('強化失敗', 'warn');
    }
  }
  await refreshAll();
}
function fx(id, text){
  const el = document.getElementById(id);
  el.textContent = text;
  el.classList.remove('fx--on');
  void el.offsetWidth;
  el.classList.add('fx--on');
}

// optional dev drop
async function devDrop(){
  // If backend has /dev/drop, use it. Otherwise just toast.
  const t = await api('/dev/drop', {method:'POST', body:{slot:'weapon'}});
  if(t.ok && t.data && t.data.success){
    toast('已生成裝備', 'ok');
    await refreshAll();
  }else{
    toast('後端未提供 /dev/drop（可自行加入）', 'info');
  }
}

// ---------------- Battle ----------------
function clearBattleUI(){
  $('#battle-log').innerHTML='';
  $('#hp-me').style.width='100%';
  $('#hp-foe').style.width='100%';
}
function appendBattle(line, kind='hit'){
  const div=document.createElement('div');
  div.className='logline logline--'+kind;
  div.textContent=line;
  $('#battle-log').appendChild(div);
  $('#battle-log').scrollTop = $('#battle-log').scrollHeight;
}
async function pvp(){
  const target = $('#battle-target').value.trim();
  $('#battle-msg').textContent='';
  if(!target){ $('#battle-msg').textContent='請輸入對手帳號'; return; }
  clearBattleUI();
  $('#bf-me').textContent = USERNAME || 'YOU';
  $('#bf-foe').textContent = target;

  const {data} = await api('/battle/pvp', {method:'POST', body:{target}});
  if(!data.success){ $('#battle-msg').textContent=data.message||'失敗'; return; }

  toast('戰鬥開始', 'info');
  // Play log
  const log = data.log || [];
  let hpMe = 100, hpFoe = 100;
  // We don't have exact max HP from backend in this response; keep relative animation.
  for(let i=0;i<log.length;i++){
    const e = log[i];
    await new Promise(r=>setTimeout(r, 180));
    const atk = e.attacker;
    const def = e.defender;
    const crit = e.crit ? ' CRIT' : '';
    appendBattle(`${atk} → ${def} 造成 ${e.damage} 傷害${crit}`, e.crit?'crit':'hit');
    if(def===target){
      hpFoe = Math.max(0, hpFoe - Math.min(18, Math.max(2, e.damage/50)));
      $('#hp-foe').style.width = Math.max(0, hpFoe) + '%';
    }else{
      hpMe = Math.max(0, hpMe - Math.min(18, Math.max(2, e.damage/50)));
      $('#hp-me').style.width = Math.max(0, hpMe) + '%';
    }
  }
  appendBattle(`勝者：${data.winner}`, 'win');
  if(data.reward){
    const rwd = data.reward;
    appendBattle(`獎勵：EXP ${rwd.exp} / GOLD ${rwd.gold}`, 'info');
    if(rwd.drop_uid) appendBattle(`掉落裝備：${rwd.drop_uid}`, 'info');
  }
  await refreshAll();
}

// ---------------- Rankings ----------------
async function loadRank(kind){
  const map = {power:'/rank/power', elo:'/rank/elo', weekly:'/rank/weekly'};
  const {data} = await api(map[kind] || '/rank/power', {auth:false});
  if(!data.success){ toast(data.message||'讀取排行失敗','warn'); return; }
  const list = $('#rank-list');
  const rows = (data.rank||[]).slice(0, 50).map((r,i)=>{
    const score = r.power ?? r.elo ?? '—';
    return `<div class="rowline">
      <div class="rowline__l"><span class="badge">#${i+1}</span> ${r.username}</div>
      <div class="rowline__r mono">${score}</div>
    </div>`;
  }).join('');
  list.innerHTML = rows || `<div class="empty">尚無排行資料</div>`;
}

// ---------------- Shop ----------------
async function loadShop(){
  const {data} = await api('/shop/list', {auth:false});
  if(!data.success){ toast(data.message||'讀取商店失敗','warn'); return; }
  const items = data.items || {};
  $('#shop-list').innerHTML = Object.entries(items).map(([id,it])=>`
    <div class="rowline">
      <div class="rowline__l">
        <div class="item__name">${it.name} <span class="badge mono">${id}</span></div>
        <div class="hint">${it.desc||''}</div>
      </div>
      <div class="rowline__r">
        <span class="badge badge--price">G ${it.price}</span>
        <button class="btn btn--ghost" data-buy="${id}">購買</button>
      </div>
    </div>`).join('');
  // bind buy
  $$('#shop-list [data-buy]').forEach(b=>{
    b.onclick = async ()=>{
      const item_id=b.dataset.buy;
      const {data} = await api('/shop/buy', {method:'POST', body:{item_id, qty:1}});
      if(!data.success){ toast(data.message||'金幣不足','warn'); return; }
      toast('購買成功', 'ok');
      await refreshAll();
    };
  });
}
async function loadInventory(){
  const {data} = await api('/shop/inventory');
  if(!data.success){ return; }
  const inv = data.inventory || {};
  $('#inv-list').innerHTML = Object.entries(inv).map(([id,qty])=>`
    <div class="rowline">
      <div class="rowline__l">${id}</div>
      <div class="rowline__r mono">x${qty}</div>
    </div>`).join('') || `<div class="empty">尚無道具</div>`;
}

// ---------------- Auction ----------------
async function loadAuctions(){
  const {data} = await api('/auction/list', {auth:false});
  if(!data.success){ toast(data.message||'讀取拍賣失敗','warn'); return; }
  const rows = (data.auctions||[]).map(a=>{
    const buyout = a.buyout_price ? ` / 直購 ${a.buyout_price}` : '';
    const title = a.type==='equip' ? `裝備 ${a.uid}` : `物品 ${a.item_id} x${a.qty}`;
    return `<div class="item">
      <div class="item__top">
        <div class="badge">${a.status||'open'}</div>
        <div class="mono">#${a.auction_id}</div>
      </div>
      <div class="item__mid">
        <div class="item__name">${title}</div>
        <div class="hint">賣家：${a.seller}｜目前：${a.current_price}${buyout}</div>
      </div>
      <div class="item__actions">
        <button class="btn btn--ghost" data-bid="${a.auction_id}">出價</button>
        <button class="btn btn--ghost" data-buy="${a.auction_id}">直購</button>
      </div>
    </div>`;
  }).join('') || `<div class="empty">目前無拍賣</div>`;
  $('#auction-list').innerHTML = rows;

  $$('#auction-list [data-bid]').forEach(btn=>{
    btn.onclick = async ()=>{
      const aid = parseInt(btn.dataset.bid,10);
      const amount = parseInt(prompt('出價金額：'),10);
      if(!amount) return;
      const {data} = await api('/auction/bid', {method:'POST', body:{auction_id:aid, bid_amount:amount}});
      if(!data.success){ toast(data.message||'出價失敗','warn'); return; }
      toast('出價成功', 'ok');
      await loadAuctions();
    };
  });
  $$('#auction-list [data-buy]').forEach(btn=>{
    btn.onclick = async ()=>{
      const aid = parseInt(btn.dataset.buy,10);
      if(!confirm('確定直購？')) return;
      const {data} = await api('/auction/buy', {method:'POST', body:{auction_id:aid}});
      if(!data.success){ toast(data.message||'直購失敗','warn'); return; }
      toast('購買成功', 'ok');
      await refreshAll();
    };
  });
}

async function createAuction(){
  const type = $('#auc-type').value;
  const item_id = $('#auc-item-id').value.trim();
  const uid = $('#auc-uid').value.trim();
  const qty = parseInt($('#auc-qty').value || '1',10);
  const start_price = parseInt($('#auc-start').value || '0',10);
  const buyout_price = ($('#auc-buyout').value.trim()==='') ? null : parseInt($('#auc-buyout').value,10);

  const body = {type, item_id, uid, qty, start_price, buyout_price};
  const {data} = await api('/auction/create', {method:'POST', body});
  if(!data.success){ $('#auction-msg').textContent = data.message||'上架失敗'; toast('上架失敗','warn'); return; }
  $('#auction-msg').textContent = '上架成功 #' + data.auction_id;
  toast('已上架', 'ok');
  await loadAuctions();
}

// ---------------- Friends ----------------
async function friendRequest(){
  const target = $('#friend-target').value.trim();
  $('#friend-msg').textContent='';
  if(!target){ $('#friend-msg').textContent='請輸入帳號'; return; }
  const {data} = await api('/friend/request', {method:'POST', body:{target}});
  if(!data.success){ $('#friend-msg').textContent=data.message||'失敗'; return; }
  toast(data.message||'已送出', 'ok');
  $('#friend-target').value='';
  await loadFriendRequests();
  await loadFriends();
}
async function loadFriendRequests(){
  const {data} = await api('/friend/requests');
  if(!data.success) return;
  const reqs = data.requests || [];
  const el = $('#friend-req-list');
  el.innerHTML = reqs.length ? reqs.map(u=>`
    <div class="rowline">
      <div class="rowline__l">${u}</div>
      <div class="rowline__r">
        <button class="btn btn--ghost" data-acc="${u}">接受</button>
        <button class="btn btn--ghost" data-rej="${u}">拒絕</button>
      </div>
    </div>`).join('') : `<div class="empty">沒有好友申請</div>`;

  $$('#friend-req-list [data-acc]').forEach(b=>b.onclick=async()=>{
    const target=b.dataset.acc;
    const {data} = await api('/friend/accept', {method:'POST', body:{target}});
    if(!data.success){ toast(data.message||'失敗','warn'); return; }
    toast('已接受', 'ok');
    await loadFriendRequests(); await loadFriends();
  });
  $$('#friend-req-list [data-rej]').forEach(b=>b.onclick=async()=>{
    const target=b.dataset.rej;
    const {data} = await api('/friend/reject', {method:'POST', body:{target}});
    if(!data.success){ toast(data.message||'失敗','warn'); return; }
    toast('已拒絕', 'info');
    await loadFriendRequests();
  });
}
async function loadFriends(){
  const {data} = await api('/friend/list');
  if(!data.success) return;
  const friends = data.friends || [];
  const el = $('#friend-list');
  el.innerHTML = friends.length ? friends.map(f=>`
    <div class="rowline">
      <div class="rowline__l">
        <span class="dot ${f.online?'dot--on':'dot--off'}"></span>
        ${f.username}
      </div>
      <div class="rowline__r">
        <span class="badge badge--price">Power ${f.power}</span>
        <button class="btn btn--ghost" data-fight="${f.username}">對戰</button>
        <button class="btn btn--ghost" data-del="${f.username}">刪除</button>
      </div>
    </div>`).join('') : `<div class="empty">尚無好友</div>`;

  $$('#friend-list [data-del]').forEach(b=>b.onclick=async()=>{
    const target=b.dataset.del;
    if(!confirm('確定刪除好友？')) return;
    const {data} = await api('/friend/remove', {method:'POST', body:{target}});
    if(!data.success){ toast(data.message||'失敗','warn'); return; }
    toast('已刪除', 'info');
    await loadFriends();
  });
  $$('#friend-list [data-fight]').forEach(b=>b.onclick=async()=>{
    const target=b.dataset.fight;
    setPage('battle');
    $('#battle-target').value = target;
    await pvp();
  });
}

// ---------------- SocketIO (3C mixed) ----------------
// Optional: If your backend serves socket.io at same origin, this will connect.
// Safe fallback if socket.io script isn't available.
function trySocket(){
  if(typeof io === 'undefined') return;
  try{
    const socket = io(API_BASE, {transports:['websocket','polling']});
    socket.on('connect', ()=>toast('Socket connected','info'));
    socket.on('announce', (msg)=>toast('公告：'+msg,'info'));
    socket.on('auction_sold', (m)=>toast('拍賣成交：'+m,'ok'));
    socket.on('friend_online', (u)=>toast(u+' 上線','info'));
  }catch(_){}
}

// ---------------- Wiring ----------------
async function refreshAll(){
  await loadMe();
  await loadEquips();
  await loadShop();
  await loadInventory();
  await loadAuctions();
  await loadFriendRequests();
  await loadFriends();
}

function bind(){
  // mini tabs
  $$('.tabs-mini__btn').forEach(b=>b.onclick=()=>setMiniTab(b.dataset.mini));
  $('#btn-login').onclick = login;
  $('#btn-register').onclick = register;

  // bottom nav
  $$('.bnav__btn').forEach(b=>b.onclick=()=>setPage(b.dataset.page));

  // top
  $('#btn-refresh').onclick = refreshAll;
  $('#btn-logout').onclick = logout;

  // profile
  $('#btn-exp-add').onclick = addExp;

  // equip
  renderSlots();
  $('#btn-equip-refresh').onclick = loadEquips;
  $('#equip-search').addEventListener('input', renderEquipList);
  $('#btn-equip-drop').onclick = devDrop;
  $('#btn-enhance').onclick = enhance;
  $('#equip-list').addEventListener('click', async (e)=>{
    const item = e.target.closest('.item');
    if(!item) return;
    const uid = item.dataset.uid;
    const act = e.target.dataset.act;
    const slot = e.target.dataset.slot || 'weapon';
    if(act==='select') setSelected(uid);
    if(act==='wear') await wearEquip(uid, slot);
  });
  $('#equip-slots').addEventListener('click', async (e)=>{
    const btn = e.target.closest('[data-act="unwear"]');
    if(!btn) return;
    await unwearSlot(btn.dataset.slot);
  });

  // battle
  $('#btn-battle').onclick = pvp;
  $('#btn-rank-power').onclick = ()=>loadRank('power');
  $('#btn-rank-elo').onclick = ()=>loadRank('elo');
  $('#btn-rank-weekly').onclick = ()=>loadRank('weekly');

  // shop
  // bind in loadShop()

  // auction
  $('#btn-auction-refresh').onclick = loadAuctions;
  $('#btn-auction-create').onclick = createAuction;

  // friends
  $('#btn-friend-request').onclick = friendRequest;
  $('#btn-friend-refresh').onclick = loadFriends;

  // enter-submit for forms
  $$('[data-enter-submit]').forEach(form=>{
    form.addEventListener('keydown', (e)=>{
      if(e.key!=='Enter') return;
      const active = document.activeElement;
      if(active && (active.tagName==='TEXTAREA')) return;
      e.preventDefault();
      // Find first primary button in same card/form
      const btn = form.querySelector('.btn--primary') || form.querySelector('button');
      if(btn) btn.click();
    });
  });
}

document.addEventListener('DOMContentLoaded', ()=>{
  bind();
  setMiniTab('login');
  setPage('profile');
  // Hide all pages except active
  setPage('profile');
  trySocket();
});

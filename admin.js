const API_BASE = window.location.origin;
let ADMIN_TOKEN = null;
let IS_ROOT = false;
const $ = (q)=>document.querySelector(q);
const $$=(q)=>Array.from(document.querySelectorAll(q));

function toast(msg){
  alert(msg);
}

async function api(path, {method='GET', body=null} = {}){
  const headers={'Content-Type':'application/json'};
  if(ADMIN_TOKEN) headers['Authorization']='Bearer '+ADMIN_TOKEN;
  const res = await fetch(API_BASE+path,{method,headers,body:body?JSON.stringify(body):null});
  let data={};
  try{ data=await res.json(); }catch(_){ data={success:false,message:'非 JSON 回應'}; }
  return {ok:res.ok,data};
}

async function login(){
  const pw = $('#ad-pass').value;
  $('#ad-msg').textContent='';
  const {data} = await api('/admin/login', {method:'POST', body:{password:pw}});
  if(!data.success){ $('#ad-msg').textContent=data.message||'登入失敗'; return; }
  ADMIN_TOKEN = data.token;
  IS_ROOT = !!data.root;
  $('#ad-msg').textContent = IS_ROOT ? 'ROOT 已登入' : 'ADMIN 已登入';
  await loadPlayers();
}

function row(k,v){ return `<div class="rowline"><div class="rowline__l">${k}</div><div class="rowline__r mono">${v}</div></div>`; }

async function loadPlayers(){
  if(!ADMIN_TOKEN){ toast('請先登入'); return; }
  const filter = ($('#ad-player-filter').value||'').trim().toLowerCase();
  const {data} = await api('/admin/players');
  if(!data.success){ toast(data.message||'失敗'); return; }
  const players = (data.players||[]).filter(p=>!filter || p.username.toLowerCase().includes(filter));
  $('#ad-players').innerHTML = players.slice(0,200).map(p=>`
    <div class="item">
      <div class="item__top">
        <div class="badge">${p.online?'ONLINE':'OFFLINE'}</div>
        <div class="mono">${p.username}</div>
      </div>
      <div class="item__mid">
        <div class="hint">G:${p.gold} / LV:${p.level} / EXP:${p.exp} / banned:${p.banned}</div>
      </div>
      <div class="item__actions">
        <button class="btn btn--ghost" data-ban="${p.username}">封鎖</button>
        <button class="btn btn--ghost" data-unban="${p.username}">解封</button>
        <button class="btn btn--ghost" data-unlock="${p.username}">解鎖登入</button>
      </div>
    </div>`).join('') || `<div class="empty">無玩家</div>`;

  $('#ad-players').querySelectorAll('[data-ban]').forEach(b=>b.onclick=()=>ban(b.dataset.ban));
  $('#ad-players').querySelectorAll('[data-unban]').forEach(b=>b.onclick=()=>unban(b.dataset.unban));
  $('#ad-players').querySelectorAll('[data-unlock]').forEach(b=>b.onclick=()=>unlock(b.dataset.unlock));
}

async function ban(u){
  const {data} = await api('/admin/ban/'+encodeURIComponent(u), {method:'POST'});
  if(!data.success){ toast(data.message||'失敗'); return; }
  toast('已封鎖 '+u);
  await loadPlayers();
}
async function unban(u){
  const {data} = await api('/admin/unban/'+encodeURIComponent(u), {method:'POST'});
  if(!data.success){ toast(data.message||'失敗'); return; }
  toast('已解封 '+u);
  await loadPlayers();
}
async function unlock(u){
  // requires root on backend in many implementations; here call /admin/unlock/<username>
  const {data} = await api('/admin/unlock/'+encodeURIComponent(u), {method:'POST'});
  if(!data.success){ toast(data.message||'失敗'); return; }
  toast('已解鎖 '+u);
}

async function announceAdd(){
  const msg = $('#ad-announce').value.trim();
  if(!msg) return;
  const {data} = await api('/admin/announce/add', {method:'POST', body:{msg}});
  if(!data.success){ toast(data.message||'失敗'); return; }
  $('#ad-announce').value='';
  await announceList();
}
async function announceList(){
  const {data} = await api('/admin/announce/list');
  if(!data.success){ toast(data.message||'失敗'); return; }
  const items = data.announcements||[];
  $('#ad-announce-list').innerHTML = items.map((m,i)=>`
    <div class="rowline">
      <div class="rowline__l">${m}</div>
      <div class="rowline__r"><button class="btn btn--ghost" data-del="${i}">刪除</button></div>
    </div>`).join('') || `<div class="empty">無公告</div>`;
  $('#ad-announce-list').querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>announceDel(b.dataset.del));
}
async function announceDel(i){
  const {data} = await api('/admin/announce/delete/'+i, {method:'DELETE'});
  if(!data.success){ toast(data.message||'失敗'); return; }
  await announceList();
}
async function announceClear(){
  if(!confirm('確定清空公告？')) return;
  const {data} = await api('/admin/announce/clear', {method:'POST'});
  if(!data.success){ toast(data.message||'失敗'); return; }
  await announceList();
}

async function loadLogs(){
  const action = $('#ad-log-action').value.trim();
  const qs = new URLSearchParams();
  qs.set('limit','120');
  if(action) qs.set('action', action);
  const {data} = await api('/admin/logs?'+qs.toString());
  if(!data.success){ toast(data.message||'失敗'); return; }
  const lines = (data.logs||[]).map(l=>`[${l.ts}] ${l.user} ${l.action} ${l.detail||''}`).join('\n');
  $('#ad-logs').textContent = lines || '無紀錄';
}
async function loadBattles(){
  const {data} = await api('/admin/battles?limit=80');
  if(!data.success){ toast(data.message||'失敗'); return; }
  const lines = (data.battles||[]).map(b=>`[${b.ts}] ${b.winner} vs ${b.loser} drop=${b.drop_uid||''}`).join('\n');
  $('#ad-battles').textContent = lines || '無紀錄';
}
async function loadAuctionSold(){
  const {data} = await api('/admin/auction/sold');
  if(!data.success){ toast(data.message||'失敗'); return; }
  const lines = (data.sold||[]).map(s=>`[${s.ts}] #${s.aid} ${s.seller} -> ${s.buyer} price=${s.price}`).join('\n');
  $('#ad-auction-sold').textContent = lines || '無紀錄';
}

document.addEventListener('DOMContentLoaded', ()=>{
  $('#btn-ad-login').onclick = login;
  $('#btn-ad-refresh').onclick = loadPlayers;
  $('#btn-ad-players').onclick = loadPlayers;
  $('#btn-ad-announce-add').onclick = announceAdd;
  $('#btn-ad-announce-list').onclick = announceList;
  $('#btn-ad-announce-clear').onclick = announceClear;
  $('#btn-ad-logs').onclick = loadLogs;
  $('#btn-ad-battles').onclick = loadBattles;
  $('#btn-ad-auction-sold').onclick = loadAuctionSold;

  // Enter submit
  document.addEventListener('keydown', (e)=>{
    if(e.key!=='Enter') return;
    const el = document.activeElement;
    if(!el) return;
    const id = el.id;
    if(id==='ad-pass') $('#btn-ad-login').click();
    if(id==='ad-announce') $('#btn-ad-announce-add').click();
    if(id==='ad-player-filter') $('#btn-ad-players').click();
    if(id==='ad-log-action') $('#btn-ad-logs').click();
  });
});

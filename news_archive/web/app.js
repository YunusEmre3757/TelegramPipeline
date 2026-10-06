'use strict';
const $ = id => document.getElementById(id);
const state = {channel:'', mode:'all', total:0, next:null, items:[], selected:null, detail:null, request:0, top:'', channels:[]};
const number = n => new Intl.NumberFormat('tr-TR').format(n);
const format = (date, full=false) => date ? new Intl.DateTimeFormat('tr-TR', {timeZone:'Europe/Istanbul', day:'2-digit',month:full?'short':'2-digit', ...(full?{year:'numeric'}:{}),hour:'2-digit',minute:'2-digit'}).format(new Date(date)) : '—';
const key = row => `${row.channel_id}:${row.message_id}`;
const articleStatus = s => ({ok:'Alındı',pending:'Kuyrukta',empty:'Metin bulunamadı',error:'İndirme hatası',skipped:'Desteklenmeyen bağlantı'}[s]||s);
function el(tag, cls, text) {const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function params(extra={}) {return new URLSearchParams({channel:state.channel,mode:state.mode,q:$('search').value,from:$('from').value,to:$('to').value,...extra});}
async function api(path) {const res=await fetch(path,{cache:'no-store'});if(!res.ok)throw new Error(res.status===400?'Tarih ve arama filtrelerini kontrol edin.':'Arşive erişilemiyor. Birazdan yeniden denenecek.');return res.json();}
function error(text='') {$('error').hidden=!text;$('error').textContent=text;}
function toast(text) {$('toast').textContent=text;$('toast').hidden=false;setTimeout(()=>$('toast').hidden=true,2500);}
function rowButton(row) {
  const b=el('button','news-row'+(state.selected===key(row)?' selected':''));b.dataset.key=key(row);
  const top=el('div','row-top');top.append(el('span','row-channel',row.title||'Kanal'));const t=el('time','',format(row.date_utc));t.dateTime=row.date_utc;top.append(t);
  const bottom=el('div','row-bottom');bottom.append(el('span',row.live?'live-label':'',row.live?'● Canlı yakalandı':'Arşiv kaydı'),el('span','','·'),el('span','',`Kayıt: ${format(row.collected_at)}`));
  b.append(top,el('p','row-preview',row.preview),bottom);b.addEventListener('click',()=>openDetail(row.channel_id,row.message_id));return b;
}
async function load(append=false) {
  const request=++state.request;
  const offset=append?state.next:0;
  $('refresh').disabled=true;$('load-more').disabled=true;
  try {
    const data=await api('/api/feed?'+params({offset:offset||0}));if(request!==state.request)return;
    error();state.total=data.total;state.next=data.next_offset;
    if(!append){state.items=[];$('news-list').replaceChildren();state.top=data.items[0]?key(data.items[0]):'';}
    const existing=new Set(state.items.map(key));
    for(const row of data.items){if(!existing.has(key(row))){state.items.push(row);$('news-list').append(rowButton(row));}}
    $('result-count').textContent=`${number(data.total)} haber · yayına göre en yeni`;
    $('empty').hidden=state.items.length>0;$('load-more').hidden=state.next===null;$('new-items').hidden=true;
    if(!state.selected&&data.items.length)openDetail(data.items[0].channel_id,data.items[0].message_id,false);
  } catch(e) {if(request===state.request)error(e.message);}
  finally {if(request===state.request){$('refresh').disabled=false;$('load-more').disabled=false;}}
}
function chooseChannel(cid) {
  state.channel=String(cid);$('feed-title').textContent=state.channels.find(c=>String(c.channel_id)===state.channel)?.title||'Haber akışı';
  document.querySelectorAll('.source').forEach(b=>b.classList.toggle('active',(b.dataset.cid||'')===state.channel));load();loadArticles();
}
async function loadArticles(){
  const channel=state.channel;
  try{
    const data=await api('/api/articles?'+new URLSearchParams({channel}));if(channel!==state.channel)return;
    const c=data.counts;
    $('article-counts').textContent=`${number(data.pending_count)} bekleyen · ${number(c.ok||0)} alınan · ${number(c.error||0)} hata · ${number(c.empty||0)} boş çıkarım`;
    for(const [id,rows] of [['pending-articles',data.pending],['recent-articles',data.recent]]){
      $(id).replaceChildren();
      for(const row of rows){const b=el('button','queue-item',row.title||row.url);b.append(el('small','',`${row.channel_title||'Kanal'} · ${articleStatus(row.status||'pending')}${row.characters?' · '+number(row.characters)+' karakter':''}`));b.title=row.error||row.url;b.addEventListener('click',()=>openDetail(row.channel_id,row.message_id));$(id).append(b);}
      if(!rows.length)$(id).append(el('p','',id==='pending-articles'?'Bekleyen URL yok.':'Henüz indirme sonucu yok.'));
    }
    if(state.detail&&data.recent.some(r=>key(r)===state.selected))openDetail(state.detail.channel_id,state.detail.message_id,false);
  }catch(e){$('article-counts').textContent=e.message;}
}
function showArticles(version){
  const root=$('article-bodies');root.replaceChildren();
  const urls=new Set(version?.urls||state.detail.urls);
  const articles=(state.detail.articles||[]).filter(a=>Array.from(urls).some(u=>{try{const n=new URL(u.startsWith('t.me/')?'https://'+u:u);n.hash='';return n.href===a.url||u===a.url;}catch{return u===a.url;}}));
  if(!articles.length){root.append(el('p','article-meta','Bu mesaj sürümünde haber bağlantısı yok.'));return;}
  for(const a of articles){const block=el('section','article-body');block.append(el('h4','',a.title||a.url),el('p','article-meta',`${articleStatus(a.status)}${a.fetched_at?' · '+format(a.fetched_at,true):''}${a.error?' · '+a.error:''}`));
    if(a.text){block.append(el('div','article-text',a.text));const copy=el('button','text-button','Haber metnini kopyala');copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(a.text);toast('Haber metni kopyalandı');}catch{toast('Metni seçerek kopyalayabilirsiniz.');}});block.append(copy);}
    else block.append(el('p','article-meta',a.status==='pending'?'Sayfa henüz indirilmedi; kuyrukta bekliyor.':'Bu bağlantıdan okunabilir haber metni alınmadı.'));
    try{const u=new URL(a.final_url||a.url);if(['http:','https:'].includes(u.protocol)){const link=el('a','','Kaynak sayfayı aç ↗');link.href=u.href;link.target='_blank';link.rel='noopener noreferrer';block.append(link);}}catch{}
    root.append(block);
  }
}
async function loadStatus() {
  try {
    const data=await api('/api/status');state.channels=data.channels;
    $('source-count').textContent=data.channels.length;$('total-count').textContent=number(data.total);
    const focused=document.activeElement?.dataset?.cid;
    const nav=document.createDocumentFragment();
    for(const c of data.channels){const b=el('button','source'+(state.channel===String(c.channel_id)?' active':''));b.dataset.cid=c.channel_id;b.append(el('span','',c.title),el('span','',number(c.count)));b.addEventListener('click',()=>chooseChannel(c.channel_id));nav.append(b);}
    $('channels').replaceChildren(nav);
    if(focused)Array.from($('channels').children).find(b=>b.dataset.cid===focused)?.focus({preventScroll:true});
    const h=data.health;const fresh=h.heartbeat_at&&(Date.now()-Date.parse(h.heartbeat_at)<120000);
    const online=fresh&&h.status==='running'&&h.connected;
    $('status-dot').classList.toggle('online',Boolean(online));
    $('status-text').textContent=online?(data.healthy?'Toplayıcı bağlı':'Bağlı · kontrol gerekli'):'Toplayıcı bağlantısını kontrol edin';
    const scanning=data.channels.find(c=>c.collection.status==='scanning');
    const waiting=data.channels.find(c=>c.collection.status==='waiting');
    const pending=data.channels.filter(c=>c.collection.status==='pending').length;
    const failures=data.channels.filter(c=>c.collection.status==='error').length;
    $('collection-status').textContent=scanning?`${scanning.title}: geçmiş taranıyor.${pending?' '+pending+' kaynak sırada.':''}`:waiting?`${waiting.title}: Telegram bekleme süresi.`:failures?`${failures} kaynağın taramasında hata var.`:online?'Yeni mesajlar dinleniyor.':'Arşiv okunabilir; toplayıcı şu an bağlı görünmüyor.';
  } catch(e) {$('status-dot').classList.remove('online');$('status-text').textContent='Sayfa sunucusuna erişilemiyor';}
}
let detailRequest=0;
async function openDetail(cid,mid,reveal=true) {
  const previousVersion=state.selected===`${cid}:${mid}`?state.detail?.versions[Number($('versions').value)]?.version_id:null;
  const request=++detailRequest;state.selected=`${cid}:${mid}`;
  document.querySelectorAll('.news-row').forEach(b=>b.classList.toggle('selected',b.dataset.key===state.selected));
  try {
    const data=await api(`/api/message?channel=${cid}&message=${mid}`);if(request!==detailRequest)return;
    state.detail=data;$('detail-empty').hidden=true;$('detail').hidden=false;
    if(reveal)$('inspector').classList.add('open');
    $('detail-channel').textContent=data.title;$('published').textContent=format(data.date_utc,true);$('observed').textContent=format(data.collected_at,true);
    $('versions').replaceChildren();data.versions.forEach((v,i)=>{const option=el('option','',i===0?'İlk gözlenen metin':`Sürüm ${i+1} · ${format(v.observed_at)}`);option.value=i;$('versions').append(option);});
    const preserved=data.versions.findIndex(v=>v.version_id===previousVersion);$('versions').value=String(preserved<0?0:preserved);showVersion();
    $('deleted-note').hidden=!data.deleted_at;$('deleted-note').textContent=data.deleted_at?`Silinme gözlendi: ${format(data.deleted_at,true)}`:'';
    $('links').replaceChildren();
    for(const value of data.urls){try{const u=new URL(value.startsWith('t.me/')?'https://'+value:value);if(!['http:','https:'].includes(u.protocol))continue;const a=el('a','',u.hostname+' ↗');a.href=u.href;a.title=u.href;a.target='_blank';a.rel='noopener noreferrer';$('links').append(a);}catch{}}
    $('telegram-link').href=data.telegram_url;$('detail-id').textContent=`Kanal ${cid} · Mesaj ${mid}`;
  }catch(e){error(e.message);}
}
function showVersion(){const data=state.detail;if(!data)return;const v=data.versions[Number($('versions').value)];$('detail-text').textContent=v?.text||(!v?data.text:'Bu sürümde metin kaldırılmış.');$('version-note').textContent=v?`${v.source==='live_new'?'Canlı kaydedildi':v.source==='live_edit'?'Düzenleme canlı yakalandı':'Tarama sırasında kaydedildi'} · ${format(v.observed_at,true)}`:'';showArticles(v);}
let polling=false;
async function poll(){if(document.hidden||polling)return;polling=true;const request=state.request;try{const data=await api('/api/feed?'+params({limit:1}));if(request!==state.request)return;const top=data.items[0]?key(data.items[0]):'';if(top!==state.top||data.total!==state.total){$('new-items').textContent=top!==state.top?'Yeni haberler var · Göster ↓':'Arşiv güncellendi · Listeyi yenile ↓';$('new-items').hidden=false;}$('last-check').textContent='Kontrol: '+new Intl.DateTimeFormat('tr-TR',{timeZone:'Europe/Istanbul',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date());}catch(e){error(e.message);}finally{polling=false;}}
let debounce;
$('filters').addEventListener('submit',e=>{e.preventDefault();load();});
$('search').addEventListener('input',()=>{clearTimeout(debounce);debounce=setTimeout(()=>load(),300);});
for(const id of ['from','to'])$(id).addEventListener('change',()=>load());
$('today').addEventListener('click',()=>{const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Istanbul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());$('from').value=day;$('to').value=day;load();});
$('reset').addEventListener('click',()=>{$('search').value='';$('from').value='';$('to').value='';load();});
$('all-sources').addEventListener('click',()=>chooseChannel(''));
$('refresh').addEventListener('click',()=>{load();loadStatus();if(state.detail)openDetail(state.detail.channel_id,state.detail.message_id,false);});
$('new-items').addEventListener('click',()=>load());$('load-more').addEventListener('click',()=>load(true));
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{state.mode=b.dataset.mode;document.querySelectorAll('.tab').forEach(t=>{t.classList.toggle('active',t===b);t.setAttribute('aria-selected',String(t===b));});load();}));
$('versions').addEventListener('change',showVersion);
function closeDetail(){detailRequest++;state.selected=null;state.detail=null;$('inspector').classList.remove('open');$('detail').hidden=true;$('detail-empty').hidden=false;document.querySelectorAll('.news-row').forEach(b=>b.classList.remove('selected'));}
$('close-detail').addEventListener('click',closeDetail);
$('copy').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('detail-text').textContent);toast('Metin kopyalandı');}catch{toast('Kopyalanamadı; metni seçerek kopyalayabilirsiniz.');}});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDetail();if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)){e.preventDefault();$('search').focus();}});
function clock(){$('clock').textContent=new Intl.DateTimeFormat('tr-TR',{timeZone:'Europe/Istanbul',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}).format(new Date());}clock();setInterval(clock,30000);
loadStatus();load();loadArticles();setInterval(poll,5000);setInterval(loadStatus,15000);setInterval(()=>{if(!document.hidden)loadArticles();},5000);document.addEventListener('visibilitychange',()=>{if(!document.hidden){poll();loadStatus();loadArticles();}});

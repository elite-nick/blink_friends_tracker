let activeChat = null;
const catalogue = new Map();

function node(tag, text, cls) {
    const e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
}
function button(label, action) {
    const b = node('button', label);
    b.type = 'button';
    b.onclick = () => Promise.resolve().then(action).catch(e => alert(e.message));
    return b;
}
function chatIcon(path) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns,'svg'), shape = document.createElementNS(ns,'path');
    svg.setAttribute('viewBox','0 0 24 24');
    svg.setAttribute('aria-hidden','true');
    shape.setAttribute('d',path);
    svg.append(shape);
    return svg;
}
function mediaURL(value) {
    try { const u = new URL(value); return ['https:', 'http:'].includes(u.protocol) ? u.href : ''; }
    catch { return ''; }
}
async function jsonRequest(url, options) {
    const response = await appFetch(url, options);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'HTTP ' + response.status);
    return data;
}
let chatPlayer = null;
function managePlayback(player) {
    if (chatPlayer && chatPlayer !== player) chatPlayer.pause();
    if (typeof currentAudio !== 'undefined' && currentAudio && currentAudio !== player) currentAudio.pause();
    chatPlayer = player;
}
document.addEventListener('play', e => {
    if (activeChat && e.target.matches?.('audio,video') && (activeChat.box.contains(e.target) || activeChat.dialog?.contains(e.target))) managePlayback(e.target);
},true);

function chatAvatar(name,id,src,cls) {
    const fallback = () => avatarPlaceholder(name,id,cls);
    if (!mediaURL(src)) return fallback();
    const img = node('img',null,cls);
    img.onerror = () => img.replaceWith(fallback());
    img.alt = ''; img.src = src;
    return img;
}
function stopChat() {
    if (chatPlayer) { chatPlayer.pause(); chatPlayer = null; }
    if (activeChat) {
        activeChat.stream?.close();
        activeChat.dialog?.remove();
    }
    activeChat = null;
    currentOpenChatId = null;
}
function updatePreview(tab,line,m) {
    const ts=Number(m?.timestamp) || 0;
    if (ts < Number(tab.dataset.timestamp || 0)) return;
    tab.dataset.timestamp=ts;
    line.firstElementChild.textContent = m?.text?.text || (m?.oneshot_media?'One-time media':m?.stickers?'Sticker':m?.orb?'Video message':m?.voice?'Voice message':m?'Media':'');
    if (ts) {
        const date=new Date(ts*1000), when=line.lastElementChild;
        when.dateTime=date.toISOString();
        when.textContent=date.toLocaleDateString('en-GB')+' · '+date.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
    }
}
function chatLoading(text) {
    const loading = node('div', null, 'chat-loading');
    loading.setAttribute('role', 'status');
    const spinner = node('span', null, 'chat-loading-spinner');
    spinner.setAttribute('aria-hidden', 'true');
    loading.append(spinner, node('span', text));
    return loading;
}
function setChatLoading(c, text) {
    c.loading?.remove();
    c.loading = text ? chatLoading(text) : null;
    c.box.setAttribute('aria-busy', String(!!text));
    if (c.loading) c.box.prepend(c.loading);
}
async function fetchChatList() {
    const list = document.getElementById('chat-list-container');
    const loading = chatLoading('Loading chats…');
    list.replaceChildren(loading);
    list.setAttribute('aria-busy', 'true');
    try {
        const seen = new Set();
        let cursor = '';
        do {
            const data = await jsonRequest('/chats'+(cursor?'?before='+cursor:''));
            if (!list.isConnected) return;
            const chats = data.items || data.chats || [];
            for (const c of chats) {
                if (seen.has(String(c.id))) continue;
                seen.add(String(c.id));
                const name = c.name || 'Chat ' + c.id;
                const last = (data.messages || []).filter(m => m.chat_id === c.id).sort((a,b) => b.timestamp-a.timestamp)[0];
                const tab = node('div', null, 'chat-list-item');
                const avatar = chatAvatar(name,c.chat_info?.peer_id || c.id,c.image,'chat-avatar');
                const info = node('div', null, 'chat-info');
                const line = node('div',null,'chat-preview-line');
                line.append(node('div',null,'chat-last-msg'),node('time',null,'chat-preview-time'));
                info.append(node('div',name,'chat-name'),line);
                updatePreview(tab,line,last);
                tab.append(avatar,info);
                tab.onclick = () => selectChat(String(c.id), name, c.image, tab);
                list.insertBefore(tab, loading);
            }
            const ids = (data.messages || []).map(m=>String(m.id)).filter(id=>/^\d+$/.test(id));
            const next = ids.reduce((min,id)=>!min || BigInt(id)<BigInt(min)?id:min,'');
            if (data.more_data === false || !next || (cursor && BigInt(next)>=BigInt(cursor))) break;
            cursor = next;
        } while (true);
        loading.remove();
        if (!list.children.length) list.textContent = 'No chats';
    } catch(e) {
        if (list.isConnected) {
            list.append(node('div', 'Could not load chats.', 'chat-load-error'));
            list.append(button('Retry loading chats', fetchChatList));
        }
    } finally {
        loading.remove();
        list.setAttribute('aria-busy', 'false');
    }
}
function selectChat(id, name, avatar, tab) {
    stopChat();
    const actor = String(myCurrentLocation?.id || '');
    if (!actor) { alert('Wait for your map marker to load, then reopen the chat.'); return; }
    const c = {id,actor,name,avatar,tab,messages:new Map(),rows:new Map(),pending:new Set(), seen:new Set(), queue:[], revealed:new Set(), opening:new Set(), ready:false};
    activeChat = c;
    currentOpenChatId = id;
    document.querySelectorAll('.chat-list-item.active').forEach(b => b.classList.remove('active'));
    tab.classList.add('active');
    const area = document.getElementById('chat-main-area');
    area.innerHTML = `<div class="chat-header"><div class="chat-header-name"></div><small class="chat-status"></small> <button id="chat-archive">Saved media</button></div>
      <div id="chat-messages-container" class="chat-messages-container"></div>
      <div class="chat-input-area">
        <div class="chat-tools">
          <button data-upload="chat-media" title="Attach a photo or video">Photo / video</button>
          <input type="file" id="chat-media" accept="image/*,video/*" hidden>
          <label class="once-toggle"><input type="checkbox" id="chat-once"> View once</label>
          <button id="chat-sticker">Sticker</button>
          <button data-upload="chat-orb">Video message</button><input id="chat-orb" type="file" accept="video/*" hidden onchange="handleOrbSelect(this)">
          <button data-upload="chat-voice">Voice</button><input id="chat-voice" type="file" accept="audio/*" hidden onchange="handleAudioSelect(this)">
          <label class="chat-font">Aa × <input id="chat-scale" inputmode="decimal" value="1.0" aria-label="Font scale"></label>
        </div>
        <div class="chat-compose"><input class="chat-input-box" id="chat-text" placeholder="Write a message…"><button id="chat-send" aria-label="Send">➤</button></div></div>`;
    for (const b of area.querySelectorAll('[data-upload]')) b.onclick=()=>document.getElementById(b.dataset.upload).click();
    const toolIcons = {
        '[data-upload="chat-media"]':'M3 5h18v14H3z M3 16l5-5 5 5 3-3 5 5 M16 8h.01',
        '[data-upload="chat-orb"]':'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z M10 8l6 4-6 4z',
        '[data-upload="chat-voice"]':'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0z M5 12a7 7 0 0 0 14 0 M12 19v3 M9 22h6',
        '#chat-sticker':'M4 4h16v10l-6 6H4z M14 20v-6h6 M8 9h.01 M14 9h.01'
    };
    for (const [selector,path] of Object.entries(toolIcons)) area.querySelector(selector).prepend(chatIcon(path));
    area.querySelector('.chat-header-name').textContent = name;
    c.status = area.querySelector('.chat-status');
    c.box = area.querySelector('#chat-messages-container');
    c.olderButton = button('Load earlier messages',()=>loadOlder(c));
    c.olderButton.className = 'chat-older';
    c.olderButton.hidden = true;
    c.box.append(c.olderButton);
    setChatLoading(c, 'Loading chat…');
    c.box.addEventListener('scroll',()=>{ if (c.box.scrollTop < 85 && c.ready && !c.olderError) loadOlder(c); });
    area.querySelector('#chat-archive').onclick = async () => {
        const dlg = chatDialog(c,'Saved media');
        const hint = node('p','Loading local archive…','dialog-note');
        dlg.append(hint);
        try {
            applySaved(c,await jsonRequest('/chats/'+c.id+'/archive?actor_id='+c.actor));
            hint.remove();
        } catch { hint.textContent = 'Could not refresh the local archive.'; }
        if (!dlg.isConnected) return;
        const saved = {...c.saved};
        for (const [id,m] of c.messages) {
            if (!m.oneshot_media) continue;
            if (!m.oneshot_media.viewed && !c.revealed.has(id) && String(m.sender_id)!==c.actor) delete saved[id];
            else if (m.oneshot_media.media.some(hasMedia)) saved[id] = mergeMedia(saved[id],m.oneshot_media.media);
        }
        const gallery = node('div',null,'archive-grid');
        for (const [id,items] of Object.entries(saved).reverse()) {
            const card = node('article',null,'archive-card');
            card.append(node('small','Message '+id));
            for (const item of items) {
                card.append(node('small',item.local_original?'Original saved locally':item.local_preview?'Only the preview is saved locally':'Only the link is available'));
                showMedia(card,[item]);
            }
            gallery.append(card);
        }
        dlg.append(gallery.children.length ? gallery : node('p','Saved photos and videos will appear here.','dialog-note'));
    };
    c.status.textContent = 'Connecting…';
    const text = area.querySelector('#chat-text'), send = area.querySelector('#chat-send');
    send.onclick = async () => {
        if (!text.value.trim() || send.disabled) return;
        const draft = text.value;
        send.disabled = true;
        try {
            const raw = area.querySelector('#chat-scale').value.trim().replace(',','.');
            const scale = Number(raw);
            if (!/^\d*\.?\d+$/.test(raw) || !Number.isFinite(scale) || scale<=0) throw new Error('Enter a positive scale, for example 1.25');
            await command(c,'send_message',{text:draft,font_scale:scale});
            if (text.value === draft) text.value = '';
        } catch(e) { alert(e.message); }
        finally { send.disabled = false; }
    };
    text.onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) send.click(); };
    area.querySelector('#chat-sticker').onclick = () => picker(c);
    area.querySelector('#chat-media').onchange = async e => {
        const input = e.target, file = input.files[0];
        if (!file) return;
        const kind = area.querySelector('#chat-once').checked ? 'send_oneshot_media_message' : 'send_media_message';
        input.disabled = true;
        try { await command(c,kind,{},file); } catch(err) { alert(err.message); }
        finally { input.disabled = false; input.value = ''; }
    };
    async function refreshStickers() {
        try { await getCatalog('stickers'); } catch {                                        }
        try { await ensureStickersLoaded(); } catch {                                   }
        if (activeChat === c) for (const m of c.messages.values()) if (m.stickers) render(c,m);
    }
    refreshStickers();
    const stream = new EventSource('/chats/'+id+'/stream?actor_id='+actor+'&account='+encodeURIComponent(pageAccount));
    c.stream = stream;
    stream.addEventListener('ready', e => syncChat(c,e.data));
    stream.onmessage = e => {
        if (activeChat !== c) return;
        const event = JSON.parse(e.data);
        if (!c.ready) c.queue.push(event);
        else { applyEvent(c,event); receipts(c); }
    };
    stream.onerror = () => {
        if (activeChat !== c) return;
        c.ready = false; c.key = null;
        c.status.textContent = 'Connection lost. Reconnecting…';
        setChatLoading(c, 'Reconnecting…');
    };
}
async function command(c,kind,fields,file) {
    if (activeChat !== c || !c.key) throw new Error('Wait for the chat to connect');
    const data = {kind, stream:c.key, actor_id:c.actor, ...fields};
    let opts = {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)};
    if (file) {
        const form = new FormData();
        Object.entries(data).forEach(([k,v]) => form.append(k,v));
        form.append('file',file);
        opts = {method:'POST',body:form};
    }
    return jsonRequest('/chats/'+c.id+'/command',opts);
}
function mergeMedia(old, fresh) {
    const items = new Map((old || []).map(i => [String(i.id),i]));
    for (const i of fresh || []) {
        const prev = items.get(String(i.id)) || {};
        items.set(String(i.id),{...prev,...i,original:i.original || prev.original,preview:i.preview || prev.preview});
    }
    return [...items.values()];
}
function applySaved(c,saved) {
    c.saved = {...c.saved,...saved};
    for (const [id,items] of Object.entries(saved || {})) {
        const m = c.messages.get(id);
        if (m?.oneshot_media) m.oneshot_media.media = mergeMedia(m.oneshot_media.media,items);
    }
}
async function syncChat(c,key) {
    if (activeChat !== c) return;
    setChatLoading(c, c.messages.size ? 'Syncing messages…' : 'Loading messages…');
    c.key = key; c.ready = false;
    const attempt = c.syncAttempt = (c.syncAttempt || 0)+1;
    const current = () => activeChat === c && c.key === key && c.syncAttempt === attempt;
    try {
        const prefix = '/chats/'+c.id, actor = 'actor_id='+c.actor;
        const previous = [...c.messages.values()].sort(order).at(-1);
        let cursor = previous?.id;
        const data = {items:[],reactions_previews:[],saved_media:{}};
        while (current()) {
            const page = await jsonRequest(prefix+'/history?'+actor+(cursor?'&after='+cursor:''));
            const items = page.items || page.messages || [];
            data.items.push(...items);
            data.reactions_previews.push(...(page.reactions_previews || []));
            Object.assign(data.saved_media,page.saved_media);
            if (!cursor || items.length < 60) break;
            const lastId = items.reduce((last,m) => BigInt(m.id)>BigInt(last)?m.id:last,cursor);
            if (lastId === cursor) break;
            cursor = lastId;
        }
        if (!current()) return;
        for (const m of data.items || data.messages || []) {
            const old = c.messages.get(String(m.id));
            if (old?.oneshot_media && m.oneshot_media) m.oneshot_media.media = mergeMedia(old.oneshot_media.media,m.oneshot_media.media);
            if (!old || (old.version || 0) <= m.version) c.messages.set(String(m.id),m);
        }
        for (const p of data.reactions_previews || []) {
            const m = c.messages.get(String(p.message_id));
            if (m) {
                m.total_count = p.total_count;
                if (Array.isArray(p.items)) m.reactions = p.items;
            }
        }
        applySaved(c,data.saved_media);
        const rows = [...c.messages.values()].sort(order);
        let warning = '';
        if (rows.length) {
            const last = rows.at(-1);
            const q = new URLSearchParams({actor_id:c.actor,first_message_id:rows[0].id,last_message_id:last.id,last_message_version:1});
            try {
                const p = await jsonRequest(prefix+'/patch?'+q);
                if (!current()) return;
                for (const ev of p.events || []) applyEvent(c,ev,false);
                applySaved(c,p.saved_media);
            } catch(e) { warning = ' · Sync error: '+e.message; }
        }
        if (!current()) return;
        for (const ev of c.queue.splice(0)) applyEvent(c,ev,false);
        c.ready = true;
        setChatLoading(c, null);
        c.status.textContent = 'Connected'+warning;
        [...c.messages.values()].sort(order).forEach(m => render(c,m));
        c.olderButton.hidden = !c.messages.size || c.noOlder;
        if (!previous) c.box.scrollTop = c.box.scrollHeight;
        receipts(c);
    } catch(e) {
        if (current()) {
            setChatLoading(c, null);
            c.status.textContent = 'History error: '+e.message+' ';
            c.status.append(button('Retry',() => syncChat(c,key)));
        }
    }
}
function order(a,b) { return a.timestamp-b.timestamp || String(a.id).length-String(b.id).length || String(a.id).localeCompare(String(b.id)); }
const newTypes = {new_message:'text',new_orb_message:'orb',new_voice_message:'voice',new_audio_sticker:'stickers',new_media_message:'media',new_oneshot_media_message:'oneshot_media'};
function applyEvent(c,ev,paint=true) {
    if (String(ev.chat_id) !== c.id) return;
    const a = ev.action_data || {}, id = String(a.message_id), kind = ev.action_type;
    if (!a.message_id) return;
    let m = c.messages.get(id);
    const field = newTypes[kind];
    if (field && !m) {
        m = {id, sender_id:ev.actor_id, sender_info:ev.sender_info,timestamp:ev.created_at,version:a.message_version};
        if (field === 'text') m.text = {text:a.text,font_settings:a.font_settings};
        else if (field === 'stickers') m.stickers = {stickers:a.stickers};
        else if (field === 'orb') m.orb = {orb_media:a.orb_media || a.orb?.orb_media || a.media};
        else if (field === 'voice') m.voice = a.voice || a;
        else m[field] = {media:a.media || [],viewed:false};
        c.messages.set(id,m);
        if (c.tab) updatePreview(c.tab,c.tab.querySelector('.chat-preview-line'),m);
    }
    if (kind === 'message_read') {
        for (const row of c.messages.values()) {
            const mine = String(row.sender_id) === c.actor;
            if (mine !== (String(ev.actor_id) === c.actor) && BigInt(row.id) <= BigInt(id) && !row.read) {
                row.read = true; if (paint) render(c,row);
            }
        }
        return;
    }
    if (!m) return;
    if (kind === 'oneshot_media_viewed' && m.oneshot_media) {
        m.oneshot_media.media = mergeMedia(m.oneshot_media.media,a.medias);
        m.oneshot_media.viewed = true;
    }
    if (kind === 'snippet_reaction_added') {
        if (c.seen.has(ev.request_id) || a.message_version <= (m.version || 0)) return;
        c.seen.add(ev.request_id);
        const counts = new Map((Array.isArray(m.reactions) ? m.reactions : []).map(r=>[String(r.reaction_key),r]));
        for (const r of a.reactions || []) {
            const key = String(r.reaction_key);
            counts.set(key,{...r,count:(counts.get(key)?.count || 0)+r.count});
            if (m.total_count != null) m.total_count += r.count;
        }
        m.reactions = [...counts.values()];
    }
    m.version = Math.max(m.version || 0,a.message_version || 0);
    if (paint) render(c,m);
}
function receipts(c) {
    if (!c.ready || !c.key || document.hidden) return;
    const incoming = [...c.messages.values()].filter(m => String(m.sender_id) !== c.actor);
    const last = incoming.filter(m => !m.read).sort(order).at(-1);
    function submit(kind,m) {
        const key = kind+':'+m.id;
        if (c.pending.has(key)) return;
        c.pending.add(key);
        command(c,kind,{message_id:m.id}).then(ev => {
            if (activeChat === c) applyEvent(c,ev);
        }).catch(e => {
            if (activeChat === c) {
                c.status.textContent = 'Read status: '+e.message+' ';
                c.status.append(button('Retry',() => {c.pending.delete(key); receipts(c);}));
            }
        });
    }
    if (last && sendReadReceipts) submit('read_message',last);
}

async function loadOlder(c) {
    if (!c.ready || c.loadingOlder || c.noOlder || !c.messages.size) return;
    c.loadingOlder = true;
    c.olderError = false;
    c.olderButton.textContent = 'Loading…';
    const first = [...c.messages.values()].sort(order)[0];
    try {
        const page = await jsonRequest('/chats/'+c.id+'/history?actor_id='+c.actor+'&before='+first.id);
        if (activeChat !== c) return;
        const older = (page.items || []).filter(m => BigInt(m.id)<BigInt(first.id) && !c.messages.has(String(m.id))).sort(order);
        const anchor = c.rows.get(String(first.id));
        const top = anchor?.getBoundingClientRect().top;
        const batch = document.createDocumentFragment();
        for (const m of older) c.messages.set(String(m.id),m);
        applySaved(c,page.saved_media);
        for (const p of page.reactions_previews || []) {
            const m = c.messages.get(String(p.message_id));
            if (m) {
                m.total_count = p.total_count;
                if (Array.isArray(p.items)) m.reactions = p.items;
            }
        }
        for (const m of older) render(c,m,batch);
        c.olderButton.after(batch);
        c.noOlder = older.length === 0;
        c.olderButton.hidden = c.noOlder;
        c.olderButton.textContent = 'Load earlier messages';
        if (anchor) c.box.scrollTop += anchor.getBoundingClientRect().top-top;
    } catch(e) {
        c.olderError = true;
        c.olderButton.textContent = 'Retry loading history';
    } finally { c.loadingOlder=false; }
}

async function openOnce(c,m) {
    const id = String(m.id);
    if (c.opening.has(id)) return;
    c.opening.add(id); render(c,m);
    try {
        if (!m.oneshot_media.viewed) {
            const ack = await command(c,'view_oneshot_media_message',{message_id:id});
            if (activeChat !== c) return;
            applyEvent(c,ack,false);
        }
        try {
            const saved = await jsonRequest('/chats/'+c.id+'/archive?actor_id='+c.actor+'&retry=0');
            if (activeChat !== c) return;
            applySaved(c,saved);
        } catch {                                                                        }
        c.revealed.add(id);
    } finally {
        c.opening.delete(id);
        if (activeChat===c) render(c,c.messages.get(id) || m);
    }
}
document.addEventListener('visibilitychange',() => { if (activeChat) receipts(activeChat); });
function archiveURL(path) {
    return typeof path==='string' && /^\/chats\/\d+\/archive\/\d+\/\d+$/.test(path) ? path : '';
}
function hasMedia(item) {
    return archiveURL(item.local_original) || archiveURL(item.local_preview) || mediaURL(item.original) || mediaURL(item.preview);
}
function showMedia(parent,items) {
    for (const item of items || []) {
        const local = archiveURL(item.local_original);
        const url = local || mediaURL(item.original);
        const preview = archiveURL(item.local_preview) || (!local && mediaURL(item.preview));
        if (!url && !preview) { parent.append(node('small','Link unavailable')); continue; }
        const video = item.local_mime?.startsWith('video/') || Number(item.type) === 2 || /\.(mp4|mov|webm|m4v)([?#]|$)/i.test(url);
        const image = node(video && url ? 'video':'img');
        image.style.cssText = 'display:block;max-width:100%;max-height:240px;border-radius:10px';
        if (video && url) {
            image.controls = true; image.preload = 'metadata'; if (preview) image.poster = preview;
            image.src = url;
        } else { image.loading = 'lazy'; image.src = preview || url; }
        parent.append(image);
        if (url) {
            const link = node('a','Open original');
            link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
            parent.append(link);
        }
    }
}
function render(c,m,batch=null) {
    const key = String(m.id), mine = String(m.sender_id) === c.actor;
    const existing = c.rows.get(key);
    const nearBottom = c.box.scrollHeight-c.box.scrollTop-c.box.clientHeight < 65;
    const row = node('div',null,'msg-row '+(mine?'me':'other'));
    if (!mine) {
        row.append(chatAvatar(m.sender_info?.name || c.name,m.sender_id,m.sender_info?.avatar || c.avatar,'msg-avatar'));
    }
    const bubble = node('div',null,'msg-bubble');
    if (m.text) {
        const text = node('span',m.text.text);
        const scale = Number(m.text.font_settings?.font_scale || 1);
        if (scale > 0 && Number.isFinite(scale)) text.style.fontSize = scale+'em';
        bubble.append(text);
    } else if (m.voice || m.orb) {
        const orb = Boolean(m.orb), media = orb ? m.orb.orb_media : m.voice;
        const player = node(orb?'video':'audio');
        const src = mediaURL(media?.original);
        if (src) player.src = src;
        player.controls = true;
        if (orb) {
            player.className = 'chat-orb-player';
            player.preload = 'metadata'; player.playsInline = true;
            if (mediaURL(media?.preview)) player.poster = media.preview;
        }
        bubble.append(player,node('small',orb?'Video message':'Voice','media-caption'));
        if (!orb && m.voice.transcription) bubble.append(node('small',m.voice.transcription,'voice-transcript'));
    } else if (m.oneshot_media || m.media) {
        if (m.oneshot_media) {
            const once = m.oneshot_media;
            const available = once.media?.some(hasMedia);
            const card=node('div',null,'once-card');
            card.append(node('strong','One-time media'));
            if (once.viewed && !available) card.append(node('small','Viewed · no copy available'));
            else if (available && c.revealed.has(key)) showMedia(card,once.media);
            else {
                const open = button(c.opening.has(key)?'Opening…':once.viewed?'Show saved media':'Open media',()=>openOnce(c,m));
                open.disabled = c.opening.has(key); card.append(open);
            }
            bubble.append(card);
        } else showMedia(bubble,m.media.media);
    } else if (m.stickers) {
        for (const s of m.stickers.stickers || []) {
            const st = (catalogue.get('stickers') || []).find(item=>Number(item.id) === Number(s.sticker_id)) || stickersData.find(item=>Number(item.id) === Number(s.sticker_id));
            if (st && mediaURL(st.img)) {
                const img = node('img'); img.width = 72; img.src = st.img; img.alt = st.name;
                if (mediaURL(st.audio)) img.onclick = () => playSound(st.audio);
                bubble.append(img);
            } else bubble.append(node('span','Sticker #'+s.sticker_id));
            bubble.append(node('small',' ×'+s.count+' '));
        }
    } else bubble.append(node('i','Unsupported message'));
    const footer = node('div',null,'message-meta');
    for (const r of Array.isArray(m.reactions)?m.reactions:[]) {
        const src = mediaURL(r.reaction_info?.preview_url);
        const chip = node('span',null,'reaction-chip');
        if (src) { const img = node('img'); img.width = 22; img.alt = '#'+r.reaction_key; img.src = src; chip.append(img); }
        else chip.append(node('small','#'+r.reaction_key));
        chip.append(node('small','×'+r.count)); footer.append(chip);
    }
    const react = button(m.total_count > 0?String(m.total_count):'',() => picker(c,m.id));
    react.prepend(chatIcon('M20 12a8 8 0 1 1-8-8 M8.5 10h.01 M14 10h.01 M8 14q4 5 8 0 M19 2v6 M16 5h6'));
    react.title = 'Add reaction'; react.className = 'react-side'; react.setAttribute('aria-label','Add reaction');
    footer.append(node('span',new Date(m.timestamp*1000).toLocaleString('en-GB',{day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'})+(mine && m.read?' ✓✓':''),'msg-time'));
    bubble.append(footer); row.append(bubble,react);
    if (existing) {
        const playing = existing.querySelector('audio,video'), next = row.querySelector('audio,video');
        if (playing && next && playing === chatPlayer && !playing.paused && playing.src === next.src) {
            existing.querySelector('.message-meta').replaceWith(footer);
            existing.querySelector('.react-side').replaceWith(react);
            return;
        }
        if (playing && next && playing.src === next.src) next.replaceWith(playing);
        existing.replaceWith(row);
    } else (batch || c.box).append(row);
    c.rows.set(key,row);
    if (!batch && nearBottom) c.box.scrollTop = c.box.scrollHeight;
}
function catalogItems(raw, sticker) {
    const found = new Map();
    function visit(o) {
        if (!o || typeof o !== 'object') return;
        if (sticker && o.sticker_task?.opened === false) return;
        if (o.id && (sticker ? o.image_url : o.preview_url || o.original_url)) {
            found.set(String(o.id),{id:o.id,img:o.image_url || o.preview_url || o.original_url,
                name:o.title || o.emoji || '#'+o.id,audio:o.audio_url});
        } else Object.values(o).forEach(visit);
    }
    visit(raw);
    return [...found.values()];
}
async function getCatalog(kind) {
    if (catalogue.has(kind)) return catalogue.get(kind);
    const data = await jsonRequest('/chat_catalog/'+kind);
    const items = catalogItems(data,kind==='stickers');
    if (items.length) catalogue.set(kind,items);
    return items;
}
function chatDialog(c,title) {
    c.dialog?.remove();
    const dlg=node('dialog',null,'chat-dialog'), header=node('header');
    const close=button('✕',()=>dlg.close());
    close.className='dialog-close';
    close.setAttribute('aria-label','Close');
    header.append(node('h3',title),close);
    dlg.append(header); c.dialog=dlg;
    dlg.setAttribute('aria-label',title);
    dlg.onclose=()=>dlg.remove();
    document.body.append(dlg); dlg.showModal();
    return dlg;
}
async function picker(c,messageId) {
    const reactions = Boolean(messageId);
    const dlg = chatDialog(c,reactions?'React to message':'Send a sticker');
    const grid = node('div',null,'choice-grid');
    const id = node('input'), count = node('input');
    for (const el of [id,count]) { el.type='number'; el.step='1'; el.min='1'; el.style.width='7em'; }
    count.value='1';
    const note = node('p','Loading…');
    const submit = button('Send',async () => {
        if (!id.value || !id.checkValidity() || !count.value || !count.checkValidity()) throw new Error('Enter an ID and a whole quantity of at least 1');
        submit.disabled = true;
        try {
            if (reactions) await command(c,'add_snippet_reaction',{message_id:messageId,snippet_id:id.value,count:count.value});
            else await command(c,'send_audio_sticker',{sticker_id:id.value,count:count.value});
            dlg.close();
        } finally { submit.disabled=false; }
    });
    const fields = node('div',null,'choice-fields');
    const idLabel=node('label','ID'), countLabel=node('label','Quantity');
    idLabel.append(id); countLabel.append(count);
    fields.append(idLabel,countLabel,submit); dlg.append(grid,note,fields);
    const known = new Set();
    function fill(items) {
        for (const item of items) {
            if (known.has(String(item.id))) continue;
            known.add(String(item.id));
            const b = button('#'+item.id,()=>{
                id.value=item.id;
                grid.querySelector('.selected')?.classList.remove('selected');
                b.classList.add('selected');
            });
            b.title=item.name;
            if (mediaURL(item.img)) { const img=node('img'); img.src=item.img; img.width=36; img.loading='lazy'; b.prepend(img); }
            grid.append(b);
        }
        note.textContent = known.size?'Choose an icon or enter an ID.':'Enter an ID manually.';
    }
    if (reactions) {
        jsonRequest('/chats/'+c.id+'/'+messageId+'/reaction-preview').then(p=>{
            const m=c.messages.get(String(messageId));
            if (activeChat===c && m && Number.isInteger(p.total_count)) {
                m.total_count=p.total_count; render(c,m);
                dlg.querySelector('h3').textContent='Message reactions · '+p.total_count;
            }
        }).catch(()=>{});
        const quick = Array.from({length:6},(_,i)=>({id:454+i,name:'Quick reaction',img:'https://chats.cdn.hexawall.ru/snippets/quick/'+i+'s'+(i===3?'_r0':'')+'.webp'}));
        fill(quick);
        await Promise.allSettled(['quick','all'].map(async k=>fill(await getCatalog(k))));
    } else {
        const source = node('select',null,'sticker-source');
        source.setAttribute('aria-label','Sticker catalog');
        source.append(new Option('My stickers','mine'),new Option('Full catalog MD','md'));
        grid.before(source);
        async function switchList() {
            const selected=source.value;
            known.clear(); grid.replaceChildren(); id.value=''; note.textContent='Loading…';
            try {
                const items=selected==='mine'?await getCatalog('stickers'):await ensureStickersLoaded();
                if (source.value===selected) fill(items);
            } catch {
                if (source.value===selected) note.textContent='Could not load the catalog. You can enter an ID manually.';
            }
        }
        source.onchange=switchList;
        await switchList();
    }
}

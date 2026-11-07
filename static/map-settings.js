const pageAccount = document.documentElement.dataset.account || '';

function readCookie(name) {
    const part = document.cookie.split('; ').find(s => s.startsWith(name+'='));
    try { return part ? JSON.parse(decodeURIComponent(part.slice(name.length+1))) : null; }
    catch { return null; }
}

function writeCookie(name,value) {
    document.cookie = name+'='+encodeURIComponent(JSON.stringify(value))+'; Path=/; Max-Age=31536000; SameSite=Lax';
}

async function appFetch(url,options={}) {
    if (!String(url).startsWith('/') || String(url).startsWith('//')) return fetch(url,options);
    const headers = new Headers(options.headers);
    headers.set('X-Blink-Account',pageAccount || 'none');
    const response = await fetch(url,{...options,headers});
    if (response.status===409) {
        const reason = await response.clone().json().catch(()=>({}));
        if (reason.account_changed) location.reload();
    }
    return response;
}

function savedView() {
    const v=readCookie('blinkStartView');
    if (v && Number.isFinite(v.lat) && Math.abs(v.lat)<=85.05113 &&
        Number.isFinite(v.lng) && Math.abs(v.lng)<=180 && Number.isInteger(v.zoom) && v.zoom>=0 && v.zoom<=19) return v;
    return null;
}

function movementBadge(geo) {
    const speed=Number(geo.speed);
    if (!Number.isFinite(speed) || speed<=0) return '';
    const bearing=Number(geo.heading);
    const arrow=geo.heading!=null && geo.heading!=='' && Number.isFinite(bearing) && bearing>=0 && bearing<=360
        ? '<span class="travel-heading"><b style="transform:rotate('+bearing+'deg)">↑</b></span>' : '';
    return arrow+'<span class="travel-speed">'+Math.max(1,Math.round(speed*3.6))+'<small>km/h</small></span>';
}

async function setupAccounts() {
    const menu=document.getElementById('account-picker'), summary=menu.querySelector('summary');
    const list=document.getElementById('account-list'), form=document.getElementById('account-form');
    const status=document.getElementById('account-status');
    function identify(user) {
        const image=chatAvatar(user.username,user.id,user.avatar,'account-avatar');
        return [image,document.createTextNode(user.username)];
    }
    async function reloadAccounts() {
        const users=await jsonRequest('/accounts');
        const active=users.find(u=>u.active);
        if ((active?.id || '')!==pageAccount) { location.reload(); return; }
        summary.replaceChildren(...(active?identify(active):[document.createTextNode('Choose an account')]),node('span','▾','account-arrow'));
        list.replaceChildren();
        for (const user of users) {
            const choice=button('',async()=>{
                if (choice.disabled) return;
                choice.disabled=true;
                try {
                    await jsonRequest('/accounts/'+user.id+'/select',{method:'POST'});
                    stopChat();
                    localStorage.setItem('blinkAccountChange',String(Date.now()));
                    location.reload();
                } catch(e) { choice.disabled=false; status.textContent=e.message; }
            });
            choice.replaceChildren(...identify(user));
            if (user.active) { choice.append(node('small','Active')); choice.disabled=true; }
            list.append(choice);
        }
        if (!active) menu.open=true;
    }
    form.onsubmit=async e=>{
        e.preventDefault();
        const submit=form.querySelector('button');
        submit.disabled=true; status.textContent='Verifying account…';
        try {
            await jsonRequest('/accounts',{method:'POST',headers:{'Content-Type':'application/json'},
                body:JSON.stringify({provider_identifier:form.elements.provider.value.trim(),token:form.elements.token.value.trim()})});
            form.reset(); await reloadAccounts(); status.textContent='Account saved. Select it above to switch.';
        } catch(err) {status.textContent=err.message;}
        finally {submit.disabled=false;}
    };
    window.addEventListener('storage',e=>{if(e.key==='blinkAccountChange') location.reload();});
    try {await reloadAccounts();} catch(e){status.textContent=e.message;}
}

const tailSheets = new Map();

async function loadTailSheet(slug) {
    const response = await appFetch('/marker-tail?' + new URLSearchParams({slug}));
    if (!response.ok) throw new Error('Tail preview unavailable');
    const data = await response.json();
    const columns = Number(data.frames_in_row), count = Number(data.frames_count);
    const duration = Number(data.duration) * 1000;
    if (!Number.isSafeInteger(columns) || columns < 1 || !Number.isSafeInteger(count) || count < 1 ||
        !Number.isFinite(duration) || (count > 1 && duration <= 0) || new URL(data.url).protocol !== 'https:') {
        throw new Error('Invalid tail spritesheet');
    }
    const image = new Image();
    image.src = data.url;
    await image.decode();
    return {image, columns, count, duration, width:image.naturalWidth / columns,
            height:image.naturalHeight / Math.ceil(count / columns)};
}

async function loadTailPreviews(event) {
    const button = event.currentTarget;
    if (button.disabled) return;
    const cells = () => document.querySelectorAll('.marker-tail[data-tail-slug]');
    const slugs = new Set(Array.from(cells(), cell => cell.dataset.tailSlug));
    const pending = Array.from(slugs).filter(slug => !tailSheets.has(slug));
    if (!pending.length) return;
    button.disabled = true;
    button.textContent = 'Loading…';
    let failed = false;
    async function worker() {
        while (pending.length) {
            const slug = pending.shift();
            try {
                const sheet = await loadTailSheet(slug);
                tailSheets.set(slug, sheet);
                for (const cell of cells()) {
                    if (cell.dataset.tailSlug === slug) showMarkerTail(cell, sheet);
                }
            } catch {
                failed = true;
            }
        }
    }
    try {
        await Promise.all([worker(), worker()]);
    } finally {
        button.disabled = false;
        button.textContent = failed ? 'Retry Marker Tails previews' : 'Load Marker Tails previews';
    }
}

function prepareMarkerTail(cell, slug) {
    if (typeof slug !== 'string' || !slug.trim()) return;
    cell.dataset.tailSlug = slug;
    cell.textContent = slug;
    const sheet = tailSheets.get(slug);
    if (sheet) showMarkerTail(cell, sheet);
}

function showMarkerTail(cell, {image, columns, count, duration, width, height}) {
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 76/width, 60/height) * Math.min(globalThis.devicePixelRatio || 1,2);
    canvas.width = Math.max(1, Math.round(width*scale));
    canvas.height = Math.max(1, Math.round(height*scale));
    canvas.className = 'tail-preview';
    canvas.setAttribute('aria-hidden','true');
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    let last = -1, handle = 0;
    function draw(frame) {
        if (frame === last) return;
        last = frame;
        ctx.clearRect(0,0,canvas.width,canvas.height);
        ctx.drawImage(image,(frame % columns)*width,Math.floor(frame/columns)*height,
                      width,height,0,0,canvas.width,canvas.height);
    }
    function play() {
        if (handle || count === 1) return;
        const start = performance.now();
        function tick(now) {
            if (!cell.isConnected) return;
            draw(Math.floor(((now-start) % duration)*count/duration));
            handle = requestAnimationFrame(tick);
        }
        handle = requestAnimationFrame(tick);
    }
    cell.prepend(canvas);
    draw(0);
    cell.addEventListener('pointerenter', play);
    cell.addEventListener('pointerleave', () => {
        cancelAnimationFrame(handle);
        handle = 0;
        draw(0);
    });
    if (cell.matches(':hover')) play();
}

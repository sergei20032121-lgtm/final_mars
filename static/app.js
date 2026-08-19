// Все не-GET запросы (команды роботу) подписываются токеном из мета-тега,
// который сервер прописывает в index.html при рендере страницы. Раньше API
// был полностью открыт всем в локальной сети без единой проверки.
(function(){
    const tokenMeta = document.querySelector('meta[name="mars-token"]');
    const MARS_TOKEN = tokenMeta ? tokenMeta.content : '';
    const _origFetch = window.fetch.bind(window);
    window.fetch = function(url, opts){
        opts = opts || {};
        const method = (opts.method || 'GET').toUpperCase();
        if (method !== 'GET' && MARS_TOKEN) {
            opts.headers = Object.assign({}, opts.headers, {'X-MARS-Token': MARS_TOKEN});
        }
        return _origFetch(url, opts);
    };
})();

const mapCanvas=document.getElementById('mapCanvas');
const mapCtx=mapCanvas.getContext('2d');
const videoImg=document.getElementById('videoImg');

let lastHumans=[],mapHumans=[],frameCount=0;
let fpsTimer=performance.now(),lastFoundCount=0;
let notifAudio=null,autopilotOn=false,lastSonarData={};

// ── ВИДЕО ──────────────────────────────────────────────────────────────────
let videoActive=false;
function videoLoop(){
    if(videoActive) return;
    videoActive=true;
    const img=new Image();
    img.onload=()=>{
        videoImg.src=img.src;
        const dot=document.getElementById('camDot');
        const st=document.getElementById('camSt');
        if(dot){dot.className='cam-dot live';}
        if(st) st.textContent='● LIVE';
        frameCount++;
        const now=performance.now();
        if(now-fpsTimer>=1000){
            const fps=(frameCount*1000/(now-fpsTimer)).toFixed(0);
            _set('fpsStat',fps+' fps'); _set('s_fps',fps);
            frameCount=0;fpsTimer=now;
        }
        videoActive=false;
        setTimeout(videoLoop,160);
    };
    img.onerror=()=>{
        const dot=document.getElementById('camDot');
        const st=document.getElementById('camSt');
        if(dot) dot.className='cam-dot';
        if(st) st.textContent='○ Нет сигнала';
        videoActive=false;
        setTimeout(videoLoop,700);
    };
    img.src='/api/camera/frame?t='+Date.now();
}

// ── КАРТА ──────────────────────────────────────────────────────────────────
let mapLoading=false;
function mapLoop(){
    const img=new Image();
    const ms=document.getElementById('mapStatus');
    img.onload=()=>{
        mapCtx.drawImage(img,0,0,mapCanvas.width,mapCanvas.height);
        URL.revokeObjectURL(img.src);
        if(ms) ms.textContent='Обновлено '+new Date().toLocaleTimeString();
    };
    fetch('/api/map').then(r=>r.blob()).then(b=>{img.src=URL.createObjectURL(b);}).catch(()=>{});
    setTimeout(mapLoop,500);
}

// ── РАДАР ──────────────────────────────────────────────────────────────────
let radarCanvas,radarCtx;
function initRadar(){
    radarCanvas=document.getElementById('radarCanvas');
    if(!radarCanvas) return;
    radarCtx=radarCanvas.getContext('2d');
    radarLoop();
}

function radarLoop(){
    if(Object.keys(lastSonarData).length>0) drawRadar(lastSonarData);
    setTimeout(radarLoop,120);
}

function drawRadar(d){
    if(!radarCtx) return;
    const W=radarCanvas.width,H=radarCanvas.height,cx=W/2,cy=H/2,R=W/2-8;

    radarCtx.fillStyle='#020a05';
    radarCtx.fillRect(0,0,W,H);
    radarCtx.save();
    radarCtx.beginPath();radarCtx.arc(cx,cy,R,0,Math.PI*2);radarCtx.clip();

    // Фон
    const bg=radarCtx.createRadialGradient(cx,cy,0,cx,cy,R);
    bg.addColorStop(0,'rgba(0,60,25,.9)');bg.addColorStop(1,'rgba(0,12,5,.98)');
    radarCtx.fillStyle=bg;radarCtx.fillRect(0,0,W,H);

    // Кольца
    [1,2,3,4].forEach(i=>{
        radarCtx.strokeStyle=i===4?'rgba(0,230,118,.45)':'rgba(0,230,118,.1)';
        radarCtx.lineWidth=i===4?1.5:.7;
        radarCtx.beginPath();radarCtx.arc(cx,cy,R*i/4,0,Math.PI*2);radarCtx.stroke();
    });

    // Линии
    radarCtx.strokeStyle='rgba(0,230,118,.08)';radarCtx.lineWidth=.7;
    for(let a=0;a<360;a+=45){
        const ar=a*Math.PI/180;
        radarCtx.beginPath();radarCtx.moveTo(cx,cy);
        radarCtx.lineTo(cx+Math.sin(ar)*R,cy-Math.cos(ar)*R);radarCtx.stroke();
    }

    // Подписи
    radarCtx.fillStyle='rgba(0,230,118,.4)';radarCtx.font='bold 8px monospace';
    ['75','150','225','300'].forEach((l,i)=>radarCtx.fillText(l,cx+3,cy-R*(i+1)/4+4));

    // Вращающийся луч
    const sw=(d.sweep_angle||0)*Math.PI/180;
    // Хвост
    for(let i=25;i>=0;i--){
        const tr=sw-i*.065;
        radarCtx.strokeStyle=`rgba(0,230,118,${((25-i)/25)*.3})`;
        radarCtx.lineWidth=2;
        radarCtx.beginPath();radarCtx.moveTo(cx,cy);
        radarCtx.lineTo(cx+Math.sin(tr)*R,cy-Math.cos(tr)*R);radarCtx.stroke();
    }
    // Луч
    radarCtx.shadowColor='#00e676';radarCtx.shadowBlur=8;
    radarCtx.strokeStyle='rgba(0,230,118,1)';radarCtx.lineWidth=2;
    radarCtx.beginPath();radarCtx.moveTo(cx,cy);
    radarCtx.lineTo(cx+Math.sin(sw)*R,cy-Math.cos(sw)*R);radarCtx.stroke();
    radarCtx.shadowBlur=0;

    // Объекты
    let objCount=0;
    const nowS=Date.now()/1000;
    for(const pt of (d.radar_points||[])){
        if(pt.dist>=270) continue;
        const ra=(pt.rel_angle||0)*Math.PI/180;
        const dr=(pt.dist/300)*R;
        const px=cx+Math.sin(ra)*dr,py=cy-Math.cos(ra)*dr;
        const age=pt.ts?Math.max(0,1-(nowS-pt.ts)/8):.5;
        if(age<.05) continue;
        objCount++;
        const cl=pt.dist<40;
        if(cl){radarCtx.shadowColor='#ff5252';radarCtx.shadowBlur=6;}
        radarCtx.fillStyle=cl?`rgba(255,82,82,${age*.85})`:`rgba(0,230,118,${age*.6})`;
        radarCtx.beginPath();radarCtx.arc(px,py,cl?4:2.5,0,Math.PI*2);radarCtx.fill();
        radarCtx.shadowBlur=0;
    }

    // Текущее препятствие
    const dist=Math.min(d.distance_cm||999,290);
    if(dist<290){
        const lx=cx+Math.sin(sw)*(dist/300)*R,ly=cy-Math.cos(sw)*(dist/300)*R;
        const cl=d.obstacle,pulse=(Math.sin(Date.now()/200)+1)/2;
        radarCtx.strokeStyle=cl?`rgba(255,82,82,${.5+pulse*.4})`:`rgba(255,171,64,${.4+pulse*.3})`;
        radarCtx.lineWidth=1.5;
        radarCtx.beginPath();radarCtx.arc(lx,ly,8+pulse*5,0,Math.PI*2);radarCtx.stroke();
        radarCtx.shadowColor=cl?'#ff5252':'#ffab40';radarCtx.shadowBlur=12;
        radarCtx.fillStyle=cl?'#ff5252':'#ffab40';
        radarCtx.beginPath();radarCtx.arc(lx,ly,cl?6:4,0,Math.PI*2);radarCtx.fill();
        radarCtx.shadowBlur=0;
        radarCtx.fillStyle='rgba(255,255,255,.85)';radarCtx.font='bold 9px monospace';
        radarCtx.fillText(dist.toFixed(0)+'см',lx+8,ly-3);
    }

    radarCtx.restore();

    // Центр
    radarCtx.shadowColor='#00e676';radarCtx.shadowBlur=10;
    radarCtx.fillStyle='#00e676';
    radarCtx.beginPath();radarCtx.arc(cx,cy,4,0,Math.PI*2);radarCtx.fill();
    radarCtx.shadowBlur=0;

    // Рамка
    radarCtx.strokeStyle='rgba(0,230,118,.5)';radarCtx.lineWidth=1.5;
    radarCtx.beginPath();radarCtx.arc(cx,cy,R,0,Math.PI*2);radarCtx.stroke();

    // Метки
    radarCtx.fillStyle='rgba(0,230,118,.5)';radarCtx.font='bold 8px monospace';
    radarCtx.fillText('С',cx-4,10);radarCtx.fillText('В',W-12,cy+4);
    radarCtx.fillText('Ю',cx-4,H-3);radarCtx.fillText('З',3,cy+4);

    radarCtx.fillStyle=d.active?'rgba(0,230,118,.7)':'rgba(255,82,82,.6)';
    radarCtx.font='bold 7px monospace';
    radarCtx.fillText(d.active?'● АКТИВЕН':'● ВЫКЛ',5,H-5);

    _set('sonarObjCount',objCount);
    _set('sonarSweep',(d.sweep_angle||0).toFixed(0)+'°');
    const badge=document.getElementById('sonarMode');
    if(badge){
        if(d.active){badge.textContent='ВКЛ';badge.style.color='var(--ac)';badge.style.background='rgba(0,230,118,.1)';badge.style.borderColor='rgba(0,230,118,.2)';}
        else{badge.textContent='ВЫКЛ';badge.style.color='var(--ac3)';badge.style.background='rgba(255,82,82,.1)';badge.style.borderColor='rgba(255,82,82,.2)';}
    }
}

// ── СОСТОЯНИЕ ─────────────────────────────────────────────────────────────
async function stateLoop(){
    try{
        const r=await fetch('/api/robot/state');
        const d=await r.json();
        const s=d.state;
        const found=s.found_humans?s.found_humans.length:0;

        // Сонар
        lastSonarData={
            distance_cm:s.sonar_dist||999,obstacle:s.sonar_obstacle||false,
            enabled:s.sonar_enabled||false,active:s.sonar_active||false,
            sweep_angle:s.sweep_angle||0,radar_points:s.radar_points||[],
            status:s.sonar_status||'ВЫКЛ',
        };

        const dist=lastSonarData.distance_cm;
        _set('sonarDist',dist>300?'>300 см':dist.toFixed(0)+' см');
        const obsEl=document.getElementById('sonarObs');
        if(obsEl){obsEl.textContent=lastSonarData.obstacle?'⚠ ПРЕПЯТСТВИЕ!':'Свободно';obsEl.style.color=lastSonarData.obstacle?'var(--ac3)':'var(--ac)';}
        const warnEl=document.getElementById('sonarWarn');
        if(warnEl){
            if(!lastSonarData.active){warnEl.textContent='📡 ВЫКЛЮЧЕН';warnEl.style.color='var(--txt2)';}
            else if(lastSonarData.obstacle){warnEl.textContent=`⚠ СТОП! ${dist.toFixed(0)} см`;warnEl.style.color='var(--ac3)';}
            else{warnEl.textContent=`📡 ${dist>300?'Чисто':dist.toFixed(0)+' см'}`;warnEl.style.color='var(--ac)';}
        }

        // Полоска
        _setv('s_gpio',s.gpio_enabled?'🟢 GPIO':'SIM',s.gpio_enabled?'':'dim');
        _set('s_batt',s.battery+'%');
        const bf=document.getElementById('batt-fill');
        if(bf){bf.style.width=s.battery+'%';bf.style.background=s.battery>30?'linear-gradient(90deg,var(--ac),#69f0ae)':s.battery>10?'linear-gradient(90deg,var(--warn),#ffcc02)':'linear-gradient(90deg,var(--ac3),#ff867c)';}
        _set('s_pos',`(${s.x.toFixed(0)},${s.y.toFixed(0)})`);
        _set('s_angle',s.angle.toFixed(0)+'°');
        _set('s_speed',s.current_speed+'/255');
        const cRu={'FORWARD':'ВПЕРЁД','BACKWARD':'НАЗАД','LEFT':'ВЛЕВО','RIGHT':'ВПРАВО','STOP':'СТОП'};
        const cCl={'FORWARD':'ac','BACKWARD':'warn','LEFT':'','RIGHT':'','STOP':'dim'};
        _setv('s_cmd',cRu[s.current_command]||s.current_command,cCl[s.current_command]||'');
        _set('s_sonar',dist>300?'—':dist.toFixed(0)+' см');
        _setv('s_found',found,found>0?'alert':'dim');
        _set('s_time',fmtTime(s.uptime));

        // Автопилот
        const aw=document.getElementById('s_auto_wrap');
        if(aw) aw.style.display=s.autopilot&&s.autopilot.enabled?'flex':'none';
        if(s.autopilot&&s.autopilot.enabled) _set('s_auto_mode',s.autopilot.mode);

        // Статус-бар
        _setv('gpioStat',s.gpio_enabled?'ON':'SIM',s.gpio_enabled?'':'w');
        _set('facesStat',s.face_count||0);
        _setv('foundStat',found,found>0?'a':'');
        _setv('s_sonar',dist>300?'—':dist.toFixed(0)+' см','');

        // Автопилот статус
        const apSt=document.getElementById('apStatus');
        if(s.autopilot&&s.autopilot.enabled){
            _set('apMode',s.autopilot.mode);
            if(apSt) apSt.style.display='block';
        } else if(!autopilotOn&&apSt){
            apSt.style.display='none';
        }

        // Info
        const ib=document.getElementById('infoBox');
        if(ib) ib.innerHTML=
            `<strong>Позиция:</strong> X=${s.x.toFixed(0)}, Y=${s.y.toFixed(0)}<br>`+
            `<strong>Угол:</strong> ${s.angle.toFixed(0)}° &nbsp; <strong>Скорость:</strong> ${s.current_speed}/255<br>`+
            `<strong>Команда:</strong> ${cRu[s.current_command]||s.current_command} &nbsp; <strong>Путь:</strong> ${s.path_length} точек`;

        // Люди
        if(s.found_humans&&s.found_humans.length!==lastHumans.length){
            lastHumans=s.found_humans;
            updateHumansList(s.found_humans);
            mapHumans=s.found_humans;
        }
        _set('humanCount',found);
        checkNewHumans(s);
        updateMotors(s);

    }catch(e){}
    setTimeout(stateLoop,700);
}

// ── СТАТИСТИКА ─────────────────────────────────────────────────────────────
async function statsLoop(){
    try{
        const r=await fetch('/api/stats');
        const d=await r.json();
        _set('st-uptime',d.uptime_str||'—'); _set('st-dist',(d.dist_m||0)+'м');
        _set('st-cover',(d.coverage_pct||0)+'%'); _set('st-humans',d.humans_found||'0');
        _set('st-path',d.path_points||'—'); _set('st-session',d.session_id||'—');
        _set('coverStat',(d.coverage_pct||0)+'%'); _set('distStat',(d.dist_m||0)+'м');
        const ld=document.getElementById('logDir');
        if(ld&&d.session_id) ld.textContent=d.session_id;
    }catch(e){}
    setTimeout(statsLoop,2500);
}

// ── ЛОГ ────────────────────────────────────────────────────────────────────
async function logLoop(){
    try{
        const r=await fetch('/api/log/events');
        const d=await r.json();
        const el=document.getElementById('eventLog');
        if(el&&d.events&&d.events.length>0){
            const clrs={
                'SESSION_START':'var(--ac)','АВТОПИЛОТ_СТАРТ':'var(--ac2)',
                'АВТОПИЛОТ_СТОП':'var(--warn)','ЧЕЛОВЕК_НАЙДЕН':'var(--ac3)',
                'ПРЕПЯТСТВИЕ':'var(--warn)','СОНАР_ВКЛ':'var(--ac)',
                'СОНАР_ВЫКЛ':'var(--txt2)','РАЗВОРОТ':'var(--ac2)',
            };
            el.innerHTML=d.events.slice().reverse().map(e=>
                `<span style="color:${clrs[e.type]||'var(--txt2)'};">[${e.time}] ${e.type}</span>`+
                (e.details?` <span style="opacity:.55;">${e.details}</span>`:'')
            ).join('<br>');
        }
    }catch(e){}
    setTimeout(logLoop,2000);
}

// ── УПРАВЛЕНИЕ ─────────────────────────────────────────────────────────────
async function cmd(c){await fetch('/api/robot/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:c})});}
async function setSpeed(v){_set('speedVal',v);await fetch('/api/robot/speed',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({speed:parseInt(v)})});}
async function clearPath(){await fetch('/api/robot/clear_path',{method:'POST'});}
function exportMap(){window.open('/api/map/export','_blank');}
function openReport(){window.open('/api/report','_blank');}

// ── WASD ────────────────────────────────────────────────────────────────────
const keysDown=new Set();
document.addEventListener('keydown',e=>{
    if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT') return;
    if(keysDown.has(e.code)) return;
    keysDown.add(e.code);
    const m={'KeyW':'forward','KeyS':'backward','KeyA':'left','KeyD':'right',
              'ArrowUp':'forward','ArrowDown':'backward','ArrowLeft':'left','ArrowRight':'right'};
    if(m[e.code]){cmd(m[e.code]);e.preventDefault();}
    if(e.code==='Space'){cmd('stop');e.preventDefault();}
    if(e.code==='KeyQ') toggleAutopilot();
    if(e.code==='KeyP') takeScreenshot();
    updateWASD();
});
document.addEventListener('keyup',e=>{
    keysDown.delete(e.code);
    const move=['KeyW','KeyS','KeyA','KeyD','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'];
    if(move.includes(e.code)) cmd('stop');
    updateWASD();
});
function updateWASD(){
    const m={'KeyW':'kW','ArrowUp':'kW','KeyS':'kS','ArrowDown':'kS',
             'KeyA':'kA','ArrowLeft':'kA','KeyD':'kD','ArrowRight':'kD','Space':'kSP'};
    for(const[code,id] of Object.entries(m)){
        const el=document.getElementById(id);
        if(el){if(keysDown.has(code)) el.classList.add('pressed');else el.classList.remove('pressed');}
    }
}

// ── АВТОПИЛОТ ───────────────────────────────────────────────────────────────
async function toggleAutopilot(){
    const btn=document.getElementById('apBtn');
    if(!autopilotOn){
        const r=await fetch('/api/autopilot/start',{method:'POST'});
        const d=await r.json();
        if(d.success){
            autopilotOn=true;
            if(btn){btn.textContent='⏹ Остановить';btn.classList.add('btn-active');}
            const apSt=document.getElementById('apStatus');
            if(apSt) apSt.style.display='block';
        }
    } else {
        await fetch('/api/autopilot/stop',{method:'POST'});
        autopilotOn=false;
        const btn2=document.getElementById('apBtn');
        if(btn2){btn2.textContent='🤖 Автопоиск';btn2.classList.remove('btn-active');}
        const apSt=document.getElementById('apStatus');
        if(apSt) apSt.style.display='none';
    }
}
async function takeScreenshot(){await fetch('/api/log/screenshot',{method:'POST'});}

// ── СОНАР ───────────────────────────────────────────────────────────────────
async function setSonarMode(mode){
    const r=await fetch('/api/sonar/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
    const d=await r.json();
    const btnOn=document.getElementById('btnSonarOn');
    if(mode==='on'&&d.success){
        if(btnOn){btnOn.classList.add('btn-active');}
    } else if(mode==='off'){
        if(btnOn) btnOn.classList.remove('btn-active');
    } else if(!d.success){
        alert(`Ошибка: ${d.message||'Неизвестная ошибка'}\n\nНажми 🔍 для диагностики`);
    }
}
async function sonarDiagnostic(){
    const el=document.getElementById('sonarDiagResult');
    if(!el) return;
    el.style.display='block';el.textContent='🔍 Диагностика...';
    try{
        const r=await fetch('/api/sonar/diagnostic');
        const d=await r.json();
        const sc={'OK':'var(--ac)','ВЫКЛ':'var(--txt2)','ОШИБКА':'var(--ac3)','НЕТ_GPIO':'var(--ac3)','ТАЙМАУТ':'var(--warn)'}[d.status]||'var(--txt2)';
        el.innerHTML=
            `<div style="color:${sc};font-weight:bold;margin-bottom:4px;">${d.verdict}</div>`+
            `GPIO: ${d.gpio_available?'✅':'❌'} &nbsp; Init: ${d.gpio_initialized?'✅':'❌'} &nbsp; Активен: ${d.active?'✅':'❌'}<br>`+
            `TRIG = Pin ${d.pin_trig} &nbsp; ECHO = Pin ${d.pin_echo}<br>`+
            `Ошибок: ${d.error_count||0}`+
            (d.last_ok_ago!==null?` &nbsp; OK: ${d.last_ok_ago}с назад`:'')+
            (d.error_msg?`<br><span style="color:var(--warn);">⚠ ${d.error_msg}</span>`:'');
    }catch(e){el.textContent='❌ Ошибка запроса';}
}

// ── КАМЕРЫ ──────────────────────────────────────────────────────────────────
async function loadCameras(){
    try{
        const r=await fetch('/api/cameras/list');
        const d=await r.json();
        const sel=document.getElementById('camSel');
        for(const c of d.devices){
            const o=document.createElement('option');
            o.value=c.device;o.textContent=c.name+' ('+c.device+')';
            sel.appendChild(o);
        }
    }catch(e){}
}
async function changeCamera(dev){
    if(!dev) return;
    await fetch('/api/cameras/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device:dev})});
}

// ── КАРТА — КЛИК ────────────────────────────────────────────────────────────
mapCanvas.addEventListener('click',e=>{
    const rect=mapCanvas.getBoundingClientRect();
    const sx=mapCanvas.width/rect.width,sy=mapCanvas.height/rect.height;
    const cx=(e.clientX-rect.left)*sx,cy=(e.clientY-rect.top)*sy;
    for(const h of mapHumans){
        if(Math.sqrt((h.x-cx)**2+(h.y-cy)**2)<18){showHuman(h.id);return;}
    }
});

function showHuman(id){
    const h=mapHumans.find(x=>x.id===id);if(!h) return;
    document.getElementById('popupContent').innerHTML=
        `<h3 style="font-family:Orbitron,sans-serif;color:var(--ac);margin-bottom:14px;font-size:13px;letter-spacing:2px;">ЧЕЛОВЕК #${id}</h3>`+
        `<img src="/api/humans/photo/${id}" onerror="this.remove()" style="width:100%;border-radius:6px;margin-bottom:12px;border:1px solid var(--brd);">`+
        `<div style="font-size:11px;color:var(--txt2);line-height:2;">`+
        `<strong style="color:var(--ac2)">Обнаружен:</strong> ${new Date(h.timestamp*1000).toLocaleTimeString()}<br>`+
        `<strong style="color:var(--ac2)">Позиция:</strong> X=${Math.round(h.x)}, Y=${Math.round(h.y)}</div>`;
    document.getElementById('popupOverlay').style.display='block';
    document.getElementById('humanPopup').style.display='block';
}
function closePopup(){
    document.getElementById('popupOverlay').style.display='none';
    document.getElementById('humanPopup').style.display='none';
}

function updateHumansList(humans){
    const list=document.getElementById('humansList');
    if(!humans.length){list.innerHTML='<div style="color:var(--txt2);padding:10px;text-align:center;font-size:11px;">Людей не обнаружено</div>';return;}
    list.innerHTML=humans.map(h=>`
        <div class="hi" onclick="showHuman(${h.id})">
            <img class="hthumb" src="/api/humans/photo/${h.id}" onerror="this.style.display='none'">
            <div class="hinfo">
                <strong>#${h.id} — Обнаружен</strong>
                (${Math.round(h.x)}, ${Math.round(h.y)}) · ${new Date(h.timestamp*1000).toLocaleTimeString()}
            </div>
        </div>`).join('');
}

// ── УВЕДОМЛЕНИЯ ─────────────────────────────────────────────────────────────
function initNotifications(){try{notifAudio=new(window.AudioContext||window.webkitAudioContext)();}catch(e){}}
function playAlert(){
    if(!notifAudio) return;
    [0,300,600].forEach(delay=>setTimeout(()=>{
        try{
            const o=notifAudio.createOscillator(),g=notifAudio.createGain();
            o.connect(g);g.connect(notifAudio.destination);
            o.frequency.value=880;o.type='square';
            g.gain.setValueAtTime(.3,notifAudio.currentTime);
            g.gain.exponentialRampToValueAtTime(.001,notifAudio.currentTime+.2);
            o.start(notifAudio.currentTime);o.stop(notifAudio.currentTime+.2);
        }catch(e){}
    },delay));
}

function showFoundAlert(count){
    // Вспышка
    const f=document.createElement('div');
    f.style.cssText='position:fixed;inset:0;z-index:9998;pointer-events:none;background:rgba(255,82,82,.2);animation:fadeFlash .8s forwards;';
    document.body.appendChild(f);
    setTimeout(()=>f.remove(),900);

    // Уведомление
    const n=document.createElement('div');
    n.className='found-notif';
    n.innerHTML=
        `<div style="font-size:30px;margin-bottom:8px;">🚨</div>`+
        `<div style="font-family:Orbitron,sans-serif;color:var(--ac3);font-size:15px;font-weight:900;letter-spacing:3px;margin-bottom:6px;">ЧЕЛОВЕК НАЙДЕН!</div>`+
        `<div style="color:var(--txt2);font-size:11px;">Обнаружено: ${count} чел. — робот останавливается</div>`+
        `<div style="color:var(--txt2);font-size:10px;margin-top:6px;opacity:.6;">нажми для закрытия</div>`;
    n.onclick=()=>n.remove();
    document.body.appendChild(n);
    setTimeout(()=>n&&n.remove(),6000);
    playAlert();

    let b=0;const orig=document.title;
    const t=setInterval(()=>{
        document.title=b++%2===0?'🚨 НАЙДЕН!':'М.А.Р.С.';
        if(b>12){clearInterval(t);document.title=orig;}
    },400);
}
function checkNewHumans(s){
    const f=s.found_humans?s.found_humans.length:0;
    if(f>lastFoundCount){showFoundAlert(f);lastFoundCount=f;}
}

// ── ТЕМА ────────────────────────────────────────────────────────────────────
function toggleTheme(){
    document.body.classList.toggle('light');
    localStorage.setItem('theme',document.body.classList.contains('light')?'light':'dark');
}
if(localStorage.getItem('theme')==='light') document.body.classList.add('light');

// ── УТИЛИТЫ ─────────────────────────────────────────────────────────────────
function _set(id,val){const el=document.getElementById(id);if(el) el.textContent=val;}
function _setv(id,val,cls){
    const el=document.getElementById(id);
    if(!el) return;
    el.textContent=val;
    // Убираем все классы val и ставим нужный
    el.className=el.className.replace(/\b(warn|alert|dim|ac)\b/g,'').trim();
    if(cls) el.classList.add(cls);
}
function fmtTime(s){const m=Math.floor(s/60);return `${m}:${String(Math.floor(s%60)).padStart(2,'0')}`;}

// ── РЕЖИМ РОБОТА ─────────────────────────────────────────────────────────────
async function setMotorSim(sim){
    await fetch('/api/motors/sim',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sim})});
    const bs=document.getElementById('btnMotorSim');
    const br=document.getElementById('btnMotorReal');
    if(sim){
        if(bs) bs.classList.add('btn-active');
        if(br) br.classList.remove('btn-active');
    } else {
        if(br) br.classList.add('btn-active');
        if(bs) bs.classList.remove('btn-active');
    }
}

async function setRobotMode(mode){
    const r=await fetch('/api/robot/mode',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
    const d=await r.json();
    const bs=document.getElementById('btnModeSim');
    const br=document.getElementById('btnModeReal');
    if(mode==='sim'||d.success){
        if(mode==='sim'){
            if(bs){bs.classList.add('btn-active');}
            if(br){br.classList.remove('btn-active');}
        } else {
            if(br){br.classList.add('btn-active');}
            if(bs){bs.classList.remove('btn-active');}
        }
        if(logger) console.log(`Режим: ${mode}`);
    } else {
        alert(`Ошибка переключения: ${d.message||'GPIO недоступен'}`);
    }
}

// ── ОБНОВЛЕНИЕ МОТОРОВ ────────────────────────────────────────────────────────
function updateMotors(s){
    const m=s.motors;
    if(!m) return;

    // Подсвечиваем активный мотор
    const cells={
        'mtr-forward':  m.forward,
        'mtr-backward': m.backward,
        'mtr-left':     m.left,
        'mtr-right':    m.right,
    };
    for(const[id,active] of Object.entries(cells)){
        const el=document.getElementById(id);
        if(el){if(active) el.classList.add('active');else el.classList.remove('active');}
    }

    // GPIO статус
    const gpioEl=document.getElementById('mtr-gpio');
    if(gpioEl){
        gpioEl.textContent=m.enabled?'🟢 Активен ('+m.status+')':'⚫ Симуляция';
        gpioEl.style.color=m.enabled?'var(--ac)':'var(--txt2)';
    }

    // Режим кнопки
    const rm=s.robot_mode||'sim';
    const bs=document.getElementById('btnModeSim');
    const br=document.getElementById('btnModeReal');
    if(rm==='real'){
        if(br&&!br.classList.contains('btn-active')) br.classList.add('btn-active');
        if(bs) bs.classList.remove('btn-active');
    } else {
        if(bs&&!bs.classList.contains('btn-active')) bs.classList.add('btn-active');
        if(br) br.classList.remove('btn-active');
    }
}

// ── KEEPALIVE — сброс watchdog каждые 800мс ──────────────────────────────────
function keepAliveLoop(){
    fetch('/api/robot/keepalive',{method:'POST'}).catch(()=>{});
    setTimeout(keepAliveLoop, 800);
}

// ── СТАРТ ────────────────────────────────────────────────────────────────────
// ── ТЕМЫ ─────────────────────────────────────────────────────────────────
function setTheme(t){
    document.body.className=document.body.className.replace(/\b(amber|night|light)\b/g,'').trim();
    if(t) document.body.classList.add(t);
    localStorage.setItem('mars-theme',t);
    document.querySelectorAll('[id^="th-"]').forEach(function(b){b.classList.remove('on');});
    const tid='th-'+(t||'steel');
    const tb=document.getElementById(tid);
    if(tb) tb.classList.add('on');
}
const savedTheme=localStorage.getItem('mars-theme')||'';
setTheme(savedTheme);

// ── E-STOP ────────────────────────────────────────────────────────────────
let estopActive=false;
async function toggleEstop(){
    estopActive=!estopActive;
    const btn=document.getElementById('estopBtn');
    await fetch('/api/robot/estop',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({activate:estopActive})});
    if(estopActive){
        if(btn){btn.textContent='🔓 РАЗБЛОКИРОВАТЬ';btn.classList.add('armed');}
        autopilotOn=false;
        const ab=document.getElementById('apBtn');
        if(ab){ab.textContent='🤖 Автопоиск';ab.classList.remove('btn-active');}
        const apSt=document.getElementById('apStatus');
        if(apSt) apSt.style.display='none';
    } else {
        if(btn){btn.textContent='⬛ АВАРИЙНЫЙ СТОП';btn.classList.remove('armed');}
    }
}

// ── МИССИЯ ───────────────────────────────────────────────────────────────
let missionActive=false;
async function startMission(){
    if(estopActive){alert('E-STOP активен! Сначала разблокируй.');return;}
    const btn=document.getElementById('missionBtn');
    if(!missionActive){
        const r=await fetch('/api/robot/mission',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({action:'start'})});
        const d=await r.json();
        if(d.success){
            missionActive=true;autopilotOn=true;
            if(btn){btn.textContent='⏹ Стоп миссия';btn.classList.add('btn-active');}
            const ab=document.getElementById('apBtn');
            if(ab){ab.textContent='⏹ Остановить';ab.classList.add('btn-active');}
            const apSt=document.getElementById('apStatus');
            if(apSt) apSt.style.display='block';
        }
    } else {
        await fetch('/api/robot/mission',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({action:'stop'})});
        missionActive=false;autopilotOn=false;
        if(btn){btn.textContent='🎯 Миссия';btn.classList.remove('btn-active');}
        const ab=document.getElementById('apBtn');
        if(ab){ab.textContent='🤖 Автопоиск';ab.classList.remove('btn-active');}
        const apSt=document.getElementById('apStatus');
        if(apSt) apSt.style.display='none';
    }
}

// ── ДЕТЕКТОР — уверенность ────────────────────────────────────────────────
function updateDetectorConfidence(faces){
    if(!faces||!faces.length) return;
    const conf=faces[0].confidence||0;
    const pct=Math.round(conf*100);
    const el=document.getElementById('facesStat');
    if(el) el.textContent=faces.length+' ('+pct+'%)';
}

// Перехватываем stateLoop для обновления confidence
const _origStateLoop=stateLoop;

loadCameras();videoLoop();mapLoop();stateLoop();initRadar();logLoop();statsLoop();initNotifications();keepAliveLoop();

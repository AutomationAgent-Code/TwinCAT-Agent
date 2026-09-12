"""img2nc_ui — 图片切片成 TwinCAT NCI G 代码的本地 Web UI。

启动:  py -m tc_template.img2nc_ui  [--port 8799] [--no-browser]

左侧面板调参(各轴行程 X/Y/Z、密度、模式、进给…),右侧实时轨迹预览 + 统计,
一键生成 / 下载 .nc。后端调用 tc_template.img2nc,contour 模式需 cv2(装在 py/3.14)。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from .img2nc import NciConfig, ImageToNci

# 允许从 JSON 传入并覆盖的 NciConfig 字段(带类型)
_FIELDS = {
    "width_mm": float, "height_mm": float, "pixel_step_mm": float, "line_step_mm": float,
    "mode": str, "dither": str, "threshold": int, "invert": bool,
    "flip_x": bool, "flip_y": bool, "gamma": float,
    "skip_white": bool, "contour_res_mm": float, "simplify_mm": float, "blur_px": float,
    "min_contour_mm": float, "z_safe": float, "z_up": float, "z_down": float, "z_top": float,
    "feed_cut": float, "feed_plunge": float, "travel_mode": str, "feed_travel": float,
    "origin": str, "serpentine": bool,
    "line_numbers": bool, "program_name": str,
}


def _cfg_from(params: dict) -> NciConfig:
    cfg = NciConfig()
    for k, typ in _FIELDS.items():
        if k in params and params[k] is not None and params[k] != "":
            try:
                setattr(cfg, k, typ(params[k]))
            except (TypeError, ValueError):
                pass
    return cfg


def _generate(image_b64: str, params: dict) -> dict:
    raw = base64.b64decode(image_b64.split(",", 1)[-1])
    img = Image.open(io.BytesIO(raw))
    img.load()
    cfg = _cfg_from(params)
    conv = ImageToNci(cfg)
    gcode = conv.convert(img)
    prev = conv.preview_image(img)
    buf = io.BytesIO()
    prev.save(buf, "PNG")
    prev_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return {"ok": True, "gcode": gcode, "preview": prev_uri,
            "grid": list(prev.size), "mode": cfg.mode}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/ping":
            self._send(200, "img2nc", "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/generate":
            self._send(404, json.dumps({"ok": False, "error": "unknown endpoint"}))
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            result = _generate(payload["image"], payload.get("params", {}))
            self._send(200, json.dumps(result))
        except Exception as e:  # noqa: BLE001
            self._send(200, json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))


def main():
    ap = argparse.ArgumentParser(description="img2nc Web UI")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[img2nc-ui] 打开 {url}  (Ctrl+C 退出)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[img2nc-ui] 已退出")
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 前端 (自包含,无外部资源)
# --------------------------------------------------------------------------- #
HTML = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>img2nc — 图片切片成 TwinCAT NCI G 代码</title>
<style>
:root{--bg:#141018;--panel:#1c1626;--panel2:#241b30;--fg:#ece6f0;--mut:#a596b5;
      --cut:#e9dcc9;--travel:#7a4a96;--accent:#c58be0;--line:#352840;--ok:#7dd3a0;--err:#ff8a8a}
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;overflow:hidden;
     background:var(--bg);color:var(--fg);font:13px/1.5 system-ui,"Segoe UI",sans-serif}
header{padding:9px 16px;background:var(--panel);border-bottom:1px solid var(--line);
       display:flex;align-items:center;gap:12px}
header h1{font-size:15px;margin:0;color:var(--accent);font-weight:600}
header .sp{flex:1}
#main{flex:1;display:flex;min-height:0}
#panel{width:322px;flex:none;overflow-y:auto;background:var(--panel);border-right:1px solid var(--line);padding:10px}
#stage{flex:1;display:flex;flex-direction:column;min-width:0}
canvas{flex:1;background:#0d0a12;width:100%;min-height:0;cursor:grab}
#stats{padding:7px 14px;background:var(--panel);border-top:1px solid var(--line);
       display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--mut)}
#stats b{color:var(--cut)}
fieldset{border:1px solid var(--line);border-radius:8px;margin:0 0 10px;padding:8px 10px 10px;background:var(--panel2)}
legend{padding:0 6px;color:var(--accent);font-weight:600;font-size:12px}
.row{display:flex;align-items:center;gap:8px;margin:6px 0}
.row label{flex:1;color:var(--mut)}
.row input[type=number],.row input[type=text],.row select{width:96px;background:#120e18;color:var(--fg);
  border:1px solid var(--line);border-radius:6px;padding:4px 7px;font-size:12px}
.row.wide input{width:100%}
input[type=range]{flex:1}
.chk{display:flex;align-items:center;gap:6px;cursor:pointer}
.chk input{width:auto}
button{background:#2a2033;color:var(--fg);border:1px solid #453357;border-radius:7px;
       padding:7px 12px;font-size:13px;cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:#1a0f24;border-color:var(--accent);font-weight:600}
button.primary:hover{filter:brightness(1.08)}
#drop{border:1.5px dashed var(--line);border-radius:8px;padding:14px;text-align:center;color:var(--mut);
      cursor:pointer;background:var(--panel2);margin-bottom:10px}
#drop.hi{border-color:var(--accent);color:var(--fg)}
#thumb{max-width:100%;max-height:120px;border-radius:6px;margin-top:8px;display:none}
.legend-sw{width:20px;height:3px;border-radius:2px;display:inline-block;vertical-align:middle}
.msg{font-size:12px;padding:2px 0}
.hint{font-size:11px;color:var(--mut);margin:-2px 0 4px}
.grp-contour,.grp-depth,.grp-travelfeed,.grp-rapid{display:none}
.busy{opacity:.55;pointer-events:none}
</style></head><body>
<header>
  <h1>img2nc</h1><span style="color:var(--mut)">图片 → TwinCAT NCI G 代码</span>
  <span id="health" title="本地服务状态" style="font-size:12px;color:var(--mut)">● 检测中</span>
  <span class="sp"></span>
  <label class="chk"><input type="checkbox" id="showTravel" checked>显示空程</label>
  <span><span class="legend-sw" style="background:var(--cut)"></span> 落笔 &nbsp;
        <span class="legend-sw" style="background:var(--travel)"></span> 空程</span>
  <button id="play">▶ 动画</button>
  <button id="reset">⟲ 复位</button>
</header>
<div id="main">
  <div id="panel">
    <div id="drop">📂 点击 / 拖拽 图片到此<br><span class="hint">PNG·JPG·BMP</span>
      <img id="thumb"></div>

    <fieldset><legend>模式</legend>
      <div class="row"><label>切片模式</label>
        <select id="mode">
          <option value="contour">contour 轮廓描边</option>
          <option value="binary">binary 点阵填实</option>
          <option value="depth">depth 灰度浮雕</option>
        </select></div>
      <div class="row grp-bin"><label>抖动</label>
        <select id="dither"><option value="floyd">floyd 误差扩散</option>
          <option value="ordered">ordered Bayer</option><option value="none">none 硬阈值</option></select></div>
      <div class="row"><label>阈值 threshold</label><input type="number" id="threshold" value="128" min="0" max="255"></div>
      <label class="chk row"><input type="checkbox" id="invert" checked><span>反色(浅色图案深底选此)</span></label>
      <label class="chk row"><input type="checkbox" id="flip_x"><span>水平镜像(左右翻转)</span></label>
      <label class="chk row"><input type="checkbox" id="flip_y"><span>垂直镜像(上下翻转)</span></label>
      <div class="row"><label>gamma</label><input type="number" id="gamma" value="1.0" step="0.1"></div>
    </fieldset>

    <fieldset><legend>轴行程 / 尺寸 (mm)</legend>
      <div class="row"><label>X 行程(宽)</label><input type="number" id="width_mm" value="120" step="1"></div>
      <div class="row"><label>Y 行程(高)</label><input type="number" id="height_mm" placeholder="自动(按比例)" step="1"></div>
      <div class="hint">Y 留空 = 按图片长宽比自动</div>
    </fieldset>

    <fieldset><legend>密度 / 分辨率 (mm)</legend>
      <div class="row grp-bin grp-depth-x"><label>点距 X</label><input type="number" id="pixel_step_mm" value="0.5" step="0.05"></div>
      <div class="row grp-bin grp-depth-x"><label>行距 Y</label><input type="number" id="line_step_mm" placeholder="同点距" step="0.05"></div>
      <div class="row grp-contour"><label>轮廓分辨率</label><input type="number" id="contour_res_mm" value="0.25" step="0.05"></div>
      <div class="row grp-contour"><label>简化容差</label><input type="number" id="simplify_mm" value="0.15" step="0.05"></div>
      <div class="row grp-contour"><label>平滑 blur</label><input type="number" id="blur_px" value="0.8" step="0.1"></div>
      <div class="row grp-contour"><label>最小轮廓</label><input type="number" id="min_contour_mm" value="1.5" step="0.5"></div>
    </fieldset>

    <fieldset><legend>Z 轴 (mm)</legend>
      <div class="row"><label>安全高 z_safe</label><input type="number" id="z_safe" value="5" step="0.5"></div>
      <div class="row"><label>悬停 z_up</label><input type="number" id="z_up" value="1" step="0.5"></div>
      <div class="row"><label>落笔 z_down</label><input type="number" id="z_down" value="-0.5" step="0.1"></div>
      <div class="row grp-depth"><label>表面 z_top</label><input type="number" id="z_top" value="0" step="0.1"></div>
    </fieldset>

    <fieldset><legend>进给 (mm/min)</legend>
      <div class="row"><label>切削 feed_cut</label><input type="number" id="feed_cut" value="1000" step="50"></div>
      <div class="row"><label>下扎 feed_plunge</label><input type="number" id="feed_plunge" value="300" step="50"></div>
      <div class="row"><label>空程方式</label>
        <select id="travel_mode"><option value="feed" selected>G1 指定速度</option>
          <option value="rapid">G0 快速</option></select></div>
      <div class="row grp-travelfeed"><label>空程进给 feed_travel</label><input type="number" id="feed_travel" value="2000" step="100"></div>
      <div class="hint">空程走太快就选"G1 指定速度",按下面进给走(可控)</div>
      <div class="row grp-rapid"><label>快速 G0 速度</label><input type="number" id="rapid_mm_min" value="6000" step="500"></div>
      <div class="row"><label>每段附加(ms)</label><input type="number" id="block_ms" value="15" step="5"></div>
      <div class="hint">此处仅用于用时预测,不影响 G 代码</div>
    </fieldset>

    <fieldset><legend>输出</legend>
      <div class="row"><label>原点</label><select id="origin">
        <option value="bottom-left">左下(Y 向上)</option><option value="top-left">左上(Y 向下)</option></select></div>
      <label class="chk row"><input type="checkbox" id="flip_x"><span>水平镜像(左右翻转)</span></label>
      <label class="chk row"><input type="checkbox" id="flip_y"><span>垂直镜像(上下翻转)</span></label>
      <div class="hint">机器出来是镜像的就勾对应方向;文字反了通常勾水平镜像</div>
      <label class="chk row"><input type="checkbox" id="serpentine" checked><span>弓字形扫描</span></label>
      <label class="chk row"><input type="checkbox" id="line_numbers"><span>输出 N 行号</span></label>
    </fieldset>

    <div class="row wide"><button class="primary" id="gen" style="flex:1">⚙ 生成预览</button></div>
    <div class="row wide"><button id="dl" style="flex:1" disabled>⬇ 下载 .nc</button></div>
    <div class="msg" id="msg"></div>
  </div>

  <div id="stage">
    <canvas id="cv"></canvas>
    <div id="stats">上传图片并点「生成预览」</div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
let imgB64=null, imgName="output", segs=[], bounds=null, gcode="", view={s:1,ox:0,oy:0}, anim=null, animI=0;

// ---- 参数分组显隐 ----
function syncMode(){
  const m=$("mode").value;
  document.querySelectorAll(".grp-contour").forEach(e=>e.style.display=(m==="contour")?"flex":"none");
  document.querySelectorAll(".grp-depth").forEach(e=>e.style.display=(m==="depth")?"flex":"none");
  document.querySelectorAll(".grp-bin,.grp-depth-x").forEach(e=>e.style.display=(m==="contour")?"none":"flex");
}
$("mode").onchange=syncMode; syncMode();

// 空程方式:选"G1 指定速度"才显示空程进给
function syncTravel(){
  const feed=$("travel_mode").value==="feed";
  document.querySelectorAll(".grp-travelfeed").forEach(e=>e.style.display=feed?"flex":"none");
  document.querySelectorAll(".grp-rapid").forEach(e=>e.style.display=feed?"none":"flex");
}
$("travel_mode").onchange=syncTravel; syncTravel();

// ---- 图片上传 ----
const drop=$("drop"), thumb=$("thumb");
drop.onclick=()=>{const i=document.createElement("input");i.type="file";i.accept="image/*";
  i.onchange=e=>loadFile(e.target.files[0]);i.click()};
drop.ondragover=e=>{e.preventDefault();drop.classList.add("hi")};
drop.ondragleave=()=>drop.classList.remove("hi");
drop.ondrop=e=>{e.preventDefault();drop.classList.remove("hi");if(e.dataTransfer.files[0])loadFile(e.dataTransfer.files[0])};
function loadFile(f){if(!f)return;imgName=(f.name||"output").replace(/\.[^.]+$/,"");
  const r=new FileReader();r.onload=()=>{imgB64=r.result;thumb.src=r.result;thumb.style.display="inline-block";
    $("msg").textContent="已载入 "+f.name+",点「生成预览」";};r.readAsDataURL(f)}

// ---- 收集参数 ----
function params(){
  const p={mode:$("mode").value,dither:$("dither").value,invert:$("invert").checked,
    flip_x:$("flip_x").checked,flip_y:$("flip_y").checked,
    serpentine:$("serpentine").checked,line_numbers:$("line_numbers").checked,origin:$("origin").value,
    travel_mode:$("travel_mode").value,
    program_name:imgName.toUpperCase().slice(0,20)};
  ["threshold","gamma","width_mm","height_mm","pixel_step_mm","line_step_mm","contour_res_mm",
   "simplify_mm","blur_px","min_contour_mm","z_safe","z_up","z_down","z_top","feed_cut","feed_plunge","feed_travel"]
   .forEach(k=>{const v=$(k).value;if(v!=="")p[k]=v});
  return p;
}

// ---- 生成 ----
$("gen").onclick=async()=>{
  if(!imgB64){$("msg").innerHTML='<span style="color:var(--err)">请先上传图片</span>';return}
  $("panel").classList.add("busy");$("msg").textContent="生成中…";
  try{
    const r=await fetch("/api/generate",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({image:imgB64,params:params()})});
    const d=await r.json();
    if(!d.ok){$("msg").innerHTML='<span style="color:var(--err)">错误: '+d.error+'</span>';return}
    gcode=d.gcode;parseG(gcode);fit();draw();stats();
    $("dl").disabled=false;
    $("msg").innerHTML='<span style="color:var(--ok)">✓ 完成 · 网格 '+d.grid.join("×")+'</span>';
  }catch(e){
    $("msg").innerHTML='<span style="color:var(--err)">✕ 无法连接本地服务</span>'+
      '<div class="hint" style="color:var(--err)">img2nc 可能已退出(托盘图标右键→退出,或已关闭)。'+
      '请重新双击 <b>img2nc.exe</b>,再刷新本页面重试。</div>';
    checkHealth();
  }
  finally{$("panel").classList.remove("busy")}
};

// ---- 服务健康检查(状态灯) ----
async function checkHealth(){
  try{
    const r=await fetch('/ping',{cache:'no-store'});
    if(r.ok){$("health").innerHTML='● 服务正常';$("health").style.color='var(--ok)';return true}
    throw 0;
  }catch(e){
    $("health").innerHTML='● 服务已断开';$("health").style.color='var(--err)';return false;
  }
}
checkHealth(); setInterval(checkHealth, 5000);

// 快速速度/换段开销改动 -> 实时刷新用时预测(无需重新生成)
["rapid_mm_min","block_ms"].forEach(id=>$(id).addEventListener("input",()=>{if(gcode)stats()}));

// ---- 下载 ----
$("dl").onclick=()=>{if(!gcode)return;
  const blob=new Blob([gcode],{type:"text/plain"});const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);a.download=imgName+".nc";a.click();};

// ---- G 代码解析 → 线段 ----
function parseG(txt){
  let x=0,y=0,z=5,g=0;segs=[];let minx=1e9,miny=1e9,maxx=-1e9,maxy=-1e9;
  for(const raw of txt.split(/\r?\n/)){const line=raw.trim();if(!line||line[0]==="(")continue;
    const gm=line.match(/G(\d+)/);if(gm)g=+gm[1];
    const xm=line.match(/X(-?[\d.]+)/),ym=line.match(/Y(-?[\d.]+)/),zm=line.match(/Z(-?[\d.]+)/);
    const nx=xm?+xm[1]:x,ny=ym?+ym[1]:y,nz=zm?+zm[1]:z;
    if(xm||ym){segs.push({x0:x,y0:y,x1:nx,y1:ny,cut:g===1&&nz<0});
      for(const p of[[x,y],[nx,ny]]){minx=Math.min(minx,p[0]);miny=Math.min(miny,p[1]);maxx=Math.max(maxx,p[0]);maxy=Math.max(maxy,p[1])}}
    x=nx;y=ny;z=nz;}
  bounds={minx,miny,maxx,maxy};
}
const cv=$("cv"),ctx=cv.getContext("2d");
function fit(){if(!bounds)return;cv.width=cv.clientWidth;cv.height=cv.clientHeight;
  const b=bounds,pad=24,w=cv.width,h=cv.height;const bw=Math.max(b.maxx-b.minx,1),bh=Math.max(b.maxy-b.miny,1);
  const s=Math.min((w-2*pad)/bw,(h-2*pad)/bh);view.s=s;view.ox=(w-bw*s)/2-b.minx*s;view.oy=(h+bh*s)/2+b.miny*s;}
const tx=x=>x*view.s+view.ox, ty=y=>-y*view.s+view.oy;
function draw(upTo){const w=cv.width,h=cv.height;ctx.clearRect(0,0,w,h);ctx.fillStyle="#0d0a12";ctx.fillRect(0,0,w,h);
  const n=upTo==null?segs.length:upTo;
  if($("showTravel").checked){ctx.strokeStyle="rgba(122,74,150,.5)";ctx.lineWidth=1;ctx.beginPath();
    for(let i=0;i<n;i++){const s=segs[i];if(!s.cut){ctx.moveTo(tx(s.x0),ty(s.y0));ctx.lineTo(tx(s.x1),ty(s.y1))}}ctx.stroke()}
  ctx.strokeStyle="#e9dcc9";ctx.lineWidth=1.4;ctx.lineCap="round";ctx.beginPath();
  for(let i=0;i<n;i++){const s=segs[i];if(s.cut){ctx.moveTo(tx(s.x0),ty(s.y0));ctx.lineTo(tx(s.x1),ty(s.y1))}}ctx.stroke();
  if(upTo!=null&&n>0&&n<segs.length){const s=segs[n-1];ctx.fillStyle="#c58be0";ctx.beginPath();ctx.arc(tx(s.x1),ty(s.y1),4,0,7);ctx.fill()}}
function fmtTime(m){ // 分钟 -> 友好字符串
  if(!isFinite(m)||m<=0)return "0秒";
  const s=Math.round(m*60);
  if(s<60)return s+"秒";
  const mm=Math.floor(s/60),ss=s%60;
  if(mm<60)return mm+"分"+(ss?ss+"秒":"");
  return Math.floor(mm/60)+"时"+(mm%60)+"分";
}
// 按 G 代码逐条移动估时:G0 用快速速度,G1 用该行 F,Z 下扎单独算,每段加换段开销
function estimateTime(txt){
  const rapid=+$("rapid_mm_min").value||3000;
  const blk=(+$("block_ms").value||0)/60000;   // ms -> 分钟
  let x=0,y=0,z=5,f=1000,g=0,tCut=0,tTrav=0,tPlunge=0,n=0;
  for(const raw of txt.split(/\r?\n/)){const line=raw.trim();if(!line||line[0]==="(")continue;
    const gm=line.match(/G(\d+)/);if(gm)g=+gm[1];
    const xm=line.match(/X(-?[\d.]+)/),ym=line.match(/Y(-?[\d.]+)/),zm=line.match(/Z(-?[\d.]+)/),fm=line.match(/F(-?[\d.]+)/);
    const nx=xm?+xm[1]:x,ny=ym?+ym[1]:y,nz=zm?+zm[1]:z;if(fm)f=+fm[1];
    const dist=Math.hypot(nx-x,ny-y,nz-z);
    if(dist>1e-9){
      if(g===0){tTrav+=dist/rapid;}
      else{const sp=f>0?f:1000; if(xm||ym)tCut+=dist/sp; else tPlunge+=dist/sp;}
      n++;
    }
    x=nx;y=ny;z=nz;
  }
  const over=n*blk;
  return {total:tCut+tTrav+tPlunge+over, cut:tCut, trav:tTrav, plunge:tPlunge, over, n};
}
function stats(){
  if(!bounds)return;
  let cut=0,trav=0;for(const s of segs){const d=Math.hypot(s.x1-s.x0,s.y1-s.y0);s.cut?cut+=d:trav+=d;}
  const b=bounds,t=estimateTime(gcode);
  $("stats").innerHTML=`尺寸 <b>${(b.maxx-b.minx).toFixed(1)}×${(b.maxy-b.miny).toFixed(1)} mm</b>`+
    ` · 线段 <b>${segs.length}</b> · 落笔 <b>${cut.toFixed(0)} mm</b> · 空程 <b>${trav.toFixed(0)} mm</b>`+
    ` · 预计用时 <b>${fmtTime(t.total)}</b> `+
    `<span style="color:var(--mut)">(切 ${fmtTime(t.cut)} · 空程 ${fmtTime(t.trav)} · 下扎 ${fmtTime(t.plunge)} · 换段 ${fmtTime(t.over)})</span>`;
}

// ---- 动画 / 复位 / 交互 ----
$("play").onclick=function(){if(anim){clearInterval(anim);anim=null;this.textContent="▶ 动画";return}
  if(!segs.length)return;animI=0;this.textContent="⏸ 暂停";const btn=this;
  anim=setInterval(()=>{animI=Math.min(segs.length,animI+Math.max(1,Math.round(segs.length/240)));
    draw(animI);if(animI>=segs.length){clearInterval(anim);anim=null;btn.textContent="▶ 动画"}},16)};
$("reset").onclick=()=>{fit();draw()};
$("showTravel").onchange=()=>draw(anim?animI:null);
let drag=null;
cv.addEventListener("mousedown",e=>{drag=[e.offsetX,e.offsetY,view.ox,view.oy];cv.style.cursor="grabbing"});
window.addEventListener("mouseup",()=>{drag=null;cv.style.cursor="grab"});
window.addEventListener("mousemove",e=>{if(!drag)return;const r=cv.getBoundingClientRect();
  view.ox=drag[2]+(e.clientX-r.left-drag[0]);view.oy=drag[3]+(e.clientY-r.top-drag[1]);draw(anim?animI:null)});
cv.addEventListener("wheel",e=>{e.preventDefault();const r=cv.getBoundingClientRect();
  const mx=e.clientX-r.left,my=e.clientY-r.top,k=e.deltaY<0?1.1:1/1.1;
  view.ox=mx-(mx-view.ox)*k;view.oy=my-(my-view.oy)*k;view.s*=k;draw(anim?animI:null)},{passive:false});
window.addEventListener("resize",()=>{if(segs.length){fit();draw()}});
</script></body></html>"""

if __name__ == "__main__":
    main()

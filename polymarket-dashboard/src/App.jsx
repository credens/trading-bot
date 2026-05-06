import React, { useState, useEffect, useCallback, useRef } from "react";

const MOCK_SC = {
  bot:"btc_scalp", initial_capital:200, current_capital:200, total_pnl:0,
  total_pnl_pct:0, win_rate:0, max_drawdown:0,
  btc_price:0, rsi:50, trend:"neutral",
  open_trades:[], closed_trades:[], cycle_log:[{time:"--:--", msg:"Esperando primer ciclo..."}],
};


const Badge = ({ text, color }) => (
  <span style={{ background:color+"22", border:`1px solid ${color}44`, color, borderRadius:6, padding:"2px 8px", fontSize:11, fontWeight:700, letterSpacing:1 }}>{text}</span>
);

const Stat = ({ label, value, color="#ccc", size=22 }) => (
  <div style={{ textAlign:"center" }}>
    <div style={{ color:"#ccc", fontSize:10, letterSpacing:1, textTransform:"uppercase", marginBottom:3 }}>{label}</div>
    <div style={{ color, fontSize:size, fontWeight:700, fontFamily:"monospace" }}>{value}</div>
  </div>
);

const PnlDisplay = ({ pnl, pct }) => {
  const pos = pnl >= 0;
  return (
    <div style={{ textAlign:"center" }}>
      <div style={{ color:"#ccc", fontSize:10, letterSpacing:1, textTransform:"uppercase", marginBottom:3 }}>P&L</div>
      <div style={{ color:pos?"#00ff88":"#ff4444", fontSize:20, fontWeight:700, fontFamily:"monospace" }}>{pos?"+":"-"}${Math.abs(pnl).toFixed(2)}</div>
      <div style={{ color:pos?"#00ff8866":"#ff444466", fontSize:11 }}>{pos?"+":"-"}{Math.abs(pct||0).toFixed(1)}%</div>
    </div>
  );
};

// ─── BTC Candlestick Chart ─────────────────────────────────────────────────────
function BTCChart({ entryPrice, side, stopLoss, defaultInterval = "1m" }) {
  const [allCandles, setAllCandles] = useState([]);
  const [interval, setIntervalVal] = useState(defaultInterval);
  const [visibleCount, setVisibleCount] = useState(80);
  const [panOffset, setPanOffset] = useState(0);
  const [yZoom, setYZoom] = useState(1);  // 1 = auto-fit, >1 = zoomed in
  const [isDragging, setIsDragging] = useState(false);
  const dragStart = useRef(null);
  const chartRef = useRef(null);
  const W = 560, H = 180, PAD = 8, YLAB = 40;
  const RSI_H = 55, MACD_H = 55, TIME_H = 18;

  useEffect(() => {
    const load = async () => {
      try {
        const r = await fetch(`https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=${interval}&limit=200`);
        const data = await r.json();
        setAllCandles(data.map(k => ({
          o: parseFloat(k[1]), h: parseFloat(k[2]),
          l: parseFloat(k[3]), c: parseFloat(k[4]),
          t: parseInt(k[0]),
        })));
      } catch { /* ignore transient market-data errors */ }
    };
    load();
    const t = setInterval(load, interval === "1m" ? 10000 : 30000);
    return () => clearInterval(t);
  }, [interval]);

  // Wheel zoom & pan
  useEffect(() => {
    const el = chartRef.current;
    if (!el) return;
    const handler = (e) => {
      e.preventDefault();
      // Detect if cursor is over Y-axis (left ~40px of chart)
      const rect = el.getBoundingClientRect();
      const relX = e.clientX - rect.left;
      const yAxisZone = rect.width * (YLAB / W);  // proportional to SVG YLAB
      if (relX < yAxisZone) {
        // Vertical zoom on price axis
        const factor = e.deltaY > 0 ? -0.15 : 0.15;
        setYZoom(prev => Math.max(0.3, Math.min(5, prev + factor)));
        return;
      }
      if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        // Pan (shift+wheel or horizontal scroll)
        const delta = (e.deltaX || e.deltaY) > 0 ? -3 : 3;
        setPanOffset(prev => Math.max(0, Math.min(prev + delta, allCandles.length - visibleCount)));
      } else {
        // Zoom (vertical wheel)
        const delta = e.deltaY > 0 ? 8 : -8;
        setVisibleCount(prev => {
          const next = Math.max(15, Math.min(200, prev + delta));
          setPanOffset(po => Math.max(0, Math.min(po, allCandles.length - next)));
          return next;
        });
      }
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, [allCandles.length, visibleCount]);

  // Drag to pan
  useEffect(() => {
    const el = chartRef.current;
    if (!el) return;
    const onDown = (e) => { dragStart.current = { x: e.clientX, offset: panOffset }; setIsDragging(true); };
    const onMove = (e) => {
      if (!dragStart.current) return;
      const dx = e.clientX - dragStart.current.x;
      const candleW = el.getBoundingClientRect().width / visibleCount;
      const shift = Math.round(dx / candleW);
      if (shift !== 0) {
        const newOffset = Math.max(0, Math.min(dragStart.current.offset + shift, allCandles.length - visibleCount));
        setPanOffset(newOffset);
      }
    };
    const onUp = () => { dragStart.current = null; setIsDragging(false); };
    el.addEventListener("mousedown", onDown);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => { el.removeEventListener("mousedown", onDown); window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
  }, [allCandles.length, visibleCount, panOffset]);

  // Slice candles for current view
  const startIdx = Math.max(0, allCandles.length - visibleCount - panOffset);
  const candles = allCandles.slice(startIdx, startIdx + visibleCount);

  if (!candles.length) return <div style={{ height: H + RSI_H + MACD_H, display:"flex", alignItems:"center", justifyContent:"center", color:"#ccc", fontSize:11 }}>Cargando gráfico...</div>;

  // ── Candlestick geometry ─────────────────────────────────────────────────────
  const prices = candles.flatMap(c => [c.h, c.l]);
  // Extend range to include SL/entry if they exist (not TP — it stretches the chart too much)
  if (entryPrice) prices.push(entryPrice);
  if (stopLoss) prices.push(stopLoss);
  const rawMinP = Math.min(...prices), rawMaxP = Math.max(...prices);
  const rawRange = rawMaxP - rawMinP || 1;
  const midP = (rawMaxP + rawMinP) / 2;
  const zoomedRange = rawRange / yZoom;
  const minP = midP - zoomedRange / 2, maxP = midP + zoomedRange / 2;
  const range = zoomedRange;
  const toY = p => PAD + (1 - (p - minP) / range) * (H - PAD * 2);
  const cW = (W - PAD - YLAB) / candles.length;

  // ── RSI (14) — compute on all data, slice to visible ─────────────────────────
  const allCloses = allCandles.map(c => c.c);
  const allRsi = (() => {
    const vals = new Array(allCloses.length).fill(null);
    if (allCloses.length < 15) return vals;
    const diffs = allCloses.slice(1).map((c, i) => c - allCloses[i]);
    let avgG = diffs.slice(0,14).filter(d=>d>0).reduce((a,b)=>a+b,0)/14;
    let avgL = diffs.slice(0,14).filter(d=>d<0).map(d=>-d).reduce((a,b)=>a+b,0)/14;
    vals[14] = avgL===0 ? 100 : 100-(100/(1+avgG/avgL));
    for (let i=14; i<diffs.length; i++) {
      const g = diffs[i]>0?diffs[i]:0, l = diffs[i]<0?-diffs[i]:0;
      avgG = (avgG*13+g)/14; avgL = (avgL*13+l)/14;
      vals[i+1] = avgL===0 ? 100 : 100-(100/(1+avgG/avgL));
    }
    return vals;
  })();
  const rsiValues = allRsi.slice(startIdx, startIdx + visibleCount);
  const rsiY = v => v===null ? null : PAD + (1 - (v - 0) / 100) * (RSI_H - PAD * 2);

  // ── MACD (12,26,9) — compute on all data, slice to visible ──────────────────
  const ema = (arr, span) => arr.reduce((acc,v,i) => i===0 ? [v] : [...acc, v*(2/(span+1)) + acc[i-1]*(1-2/(span+1))], []);
  const allEma12 = ema(allCloses, 12), allEma26 = ema(allCloses, 26);
  const allMacd = allEma12.map((v,i) => v - allEma26[i]);
  const allSignal = ema(allMacd, 9);
  const allHist = allMacd.map((v,i) => v - allSignal[i]);
  const macdLine = allMacd.slice(startIdx, startIdx + visibleCount);
  const signal = allSignal.slice(startIdx, startIdx + visibleCount);
  const histogram = allHist.slice(startIdx, startIdx + visibleCount);
  const macdMin = Math.min(...histogram), macdMax = Math.max(...histogram);
  const macdRange = Math.max(Math.abs(macdMin), Math.abs(macdMax)) * 2 || 1;
  const macdY = v => PAD + (1 - (v - (-macdRange/2)) / macdRange) * (MACD_H - PAD * 2);
  const macdZeroY = macdY(0);

  const xCenter = i => YLAB + i * cW + cW / 2;

  return (
    <div ref={chartRef} style={{ marginBottom:14, cursor: isDragging ? "grabbing" : "grab", userSelect:"none" }}>
      <div style={{ display:"flex", gap:4, marginBottom:6, alignItems:"center" }}>
        {["1m","5m","15m","1h"].map(iv => (
          <button key={iv} onClick={()=>{setIntervalVal(iv); setVisibleCount(80); setPanOffset(0); setYZoom(1);}} style={{ background:interval===iv?"rgba(255,184,0,0.15)":"transparent", border:`1px solid ${interval===iv?"#ffb80055":"#333"}`, color:interval===iv?"#ffb800":"#bbb", borderRadius:5, padding:"3px 8px", fontSize:10, cursor:"pointer", fontFamily:"monospace" }}>{iv}</button>
        ))}
        <span style={{ color:"#bbb", fontSize:10, marginLeft:8 }}>
          ${candles[candles.length-1]?.c.toLocaleString()}
        </span>
        <span style={{ color:"#555", fontSize:9, marginLeft:"auto" }}>
          {visibleCount < 200 || panOffset > 0 ? `${visibleCount} velas` : ""}
          {yZoom !== 1 && <span style={{ color:"#f0c040", marginLeft:4 }}>Y:{yZoom.toFixed(1)}x</span>}
          {(visibleCount !== 80 || yZoom !== 1) && <button onClick={()=>{setVisibleCount(80);setPanOffset(0);setYZoom(1);}} style={{ background:"transparent", border:"1px solid #444", color:"#888", borderRadius:4, padding:"1px 6px", fontSize:9, cursor:"pointer", fontFamily:"monospace", marginLeft:4 }}>reset</button>}
        </span>
      </div>

      {/* ── Candlestick ─────────────────────────────────────────────────────── */}
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ background:"rgba(0,0,0,0.2)", borderRadius:"8px 8px 0 0", display:"block" }}>
        {/* Y-axis zoom zone cursor */}
        <rect x={0} y={0} width={YLAB} height={H} fill="rgba(0,0,0,0.01)" style={{ cursor:"ns-resize" }} />
        {[0,0.25,0.5,0.75,1].map(f => {
          const price = maxP - f * range, y = PAD + f * (H - PAD * 2);
          return (
            <g key={f}>
              <line x1={PAD+36} y1={y} x2={W-PAD} y2={y} stroke="#ffffff08" strokeWidth="1" />
              <text x={PAD+34} y={y+3} fill="#ccc" fontSize="8" textAnchor="end" style={{ cursor:"ns-resize" }}>{price>=1000?`${(price/1000).toFixed(1)}k`:price.toFixed(0)}</text>
            </g>
          );
        })}
        {entryPrice && (
          <>
            <line x1={PAD+36} y1={toY(entryPrice)} x2={W-PAD} y2={toY(entryPrice)} stroke={side==="LONG"?"#00ff8866":"#ff444466"} strokeWidth="1" strokeDasharray="4,4" />
            <text x={W-PAD-2} y={toY(entryPrice)-3} fill={side==="LONG"?"#00ff88":"#ff4444"} fontSize="8" textAnchor="end">entry ${entryPrice>=1000?(entryPrice/1000).toFixed(2)+"k":entryPrice.toFixed(2)}</text>
          </>
        )}
        {stopLoss && stopLoss >= minP && stopLoss <= maxP && (
          <>
            <line x1={PAD+36} y1={toY(stopLoss)} x2={W-PAD} y2={toY(stopLoss)} stroke="#ff4444" strokeWidth="1.2" strokeDasharray="2,3" />
            <rect x={W-PAD-52} y={toY(stopLoss)-8} width={50} height={12} rx={3} fill="#ff444433" />
            <text x={W-PAD-4} y={toY(stopLoss)+1} fill="#ff4444" fontSize="8" fontWeight="bold" textAnchor="end">SL ${stopLoss>=1000?(stopLoss/1000).toFixed(2)+"k":stopLoss.toFixed(2)}</text>
          </>
        )}
        {/* TP line removed — stretches Y-axis too much when far from current price */}
        {candles.map((c,i) => {
          const x = YLAB + i*cW + cW*0.1, w = cW*0.8, cx = xCenter(i);
          const bullish = c.c >= c.o, color = bullish?"#00ff88":"#ff4444";
          const bodyTop = toY(Math.max(c.o,c.c)), bodyH = Math.max(1,Math.abs(toY(c.o)-toY(c.c)));
          return (
            <g key={i}>
              <line x1={cx} y1={toY(c.h)} x2={cx} y2={toY(c.l)} stroke={color} strokeWidth="0.8" opacity="0.7" />
              <rect x={x} y={bodyTop} width={w} height={bodyH} fill={color} opacity={bullish?0.8:0.9} />
            </g>
          );
        })}
      </svg>

      {/* ── Time axis ───────────────────────────────────────────────────────── */}
      <svg width="100%" viewBox={`0 0 ${W} ${TIME_H}`} style={{ background:"rgba(0,0,0,0.18)", display:"block" }}>
        {candles.map((c, i) => {
          const step = Math.max(1, Math.floor(candles.length / 6));
          if (i % step !== 0) return null;
          const d = new Date(c.t);
          const label = `${d.getHours().toString().padStart(2,"0")}:${d.getMinutes().toString().padStart(2,"0")}`;
          return (
            <text key={i} x={xCenter(i)} y={TIME_H - 4} fill="#666" fontSize="7" textAnchor="middle">{label}</text>
          );
        })}
      </svg>

      {/* ── RSI ─────────────────────────────────────────────────────────────── */}
      <svg width="100%" viewBox={`0 0 ${W} ${RSI_H}`} style={{ background:"rgba(0,0,0,0.15)", display:"block" }}>
        <text x={PAD+34} y={PAD+8} fill="#888" fontSize="8" textAnchor="end">RSI</text>
        {/* 70 / 30 levels */}
        {[70,50,30].map(lvl => {
          const y = rsiY(lvl);
          return <line key={lvl} x1={YLAB} y1={y} x2={W-PAD} y2={y} stroke={lvl===50?"#ffffff10":lvl===70?"#ff444422":"#00ff8822"} strokeWidth={lvl===50?0.5:1} strokeDasharray={lvl===50?"":"3,3"} />;
        })}
        <text x={YLAB-2} y={rsiY(70)+3} fill="#ff4444" fontSize="7" textAnchor="end">70</text>
        <text x={YLAB-2} y={rsiY(30)+3} fill="#00ff88" fontSize="7" textAnchor="end">30</text>
        {/* RSI line */}
        {rsiValues.map((v,i) => {
          if (v===null || rsiValues[i-1]===null) return null;
          const color = v>70?"#ff4444":v<30?"#00ff88":"#888bff";
          return <line key={i} x1={xCenter(i-1)} y1={rsiY(rsiValues[i-1])} x2={xCenter(i)} y2={rsiY(v)} stroke={color} strokeWidth="1.2" opacity="0.9" />;
        })}
        {/* Current RSI value */}
        {(() => { const last = rsiValues.filter(v=>v!==null).pop(); if(!last) return null;
          const color = last>70?"#ff4444":last<30?"#00ff88":"#888bff";
          return <text x={W-PAD} y={rsiY(last)+3} fill={color} fontSize="8" textAnchor="end">{last.toFixed(0)}</text>;
        })()}
      </svg>

      {/* ── MACD ────────────────────────────────────────────────────────────── */}
      <svg width="100%" viewBox={`0 0 ${W} ${MACD_H}`} style={{ background:"rgba(0,0,0,0.1)", borderRadius:"0 0 8px 8px", display:"block" }}>
        <text x={PAD+34} y={PAD+8} fill="#888" fontSize="8" textAnchor="end">MACD</text>
        {/* Zero line */}
        <line x1={YLAB} y1={macdZeroY} x2={W-PAD} y2={macdZeroY} stroke="#ffffff15" strokeWidth="0.8" />
        {/* Histogram bars */}
        {histogram.map((v,i) => {
          const y = macdY(v), h = Math.abs(macdZeroY - y) || 1;
          const color = v>=0?"#00ff8866":"#ff444466";
          return <rect key={i} x={YLAB+i*cW+cW*0.1} y={Math.min(y,macdZeroY)} width={cW*0.8} height={h} fill={color} />;
        })}
        {/* MACD line */}
        {macdLine.map((v,i) => {
          if (i===0) return null;
          return <line key={i} x1={xCenter(i-1)} y1={macdY(macdLine[i-1])} x2={xCenter(i)} y2={macdY(v)} stroke="#ffb800" strokeWidth="1" opacity="0.9" />;
        })}
        {/* Signal line */}
        {signal.map((v,i) => {
          if (i===0) return null;
          return <line key={i} x1={xCenter(i-1)} y1={macdY(signal[i-1])} x2={xCenter(i)} y2={macdY(v)} stroke="#ff88cc" strokeWidth="1" opacity="0.9" />;
        })}
        {/* Legend */}
        <line x1={W-70} y1={MACD_H-8} x2={W-60} y2={MACD_H-8} stroke="#ffb800" strokeWidth="1.5" />
        <text x={W-58} y={MACD_H-5} fill="#ffb800" fontSize="7">MACD</text>
        <line x1={W-30} y1={MACD_H-8} x2={W-20} y2={MACD_H-8} stroke="#ff88cc" strokeWidth="1.5" />
        <text x={W-18} y={MACD_H-5} fill="#ff88cc" fontSize="7">Sig</text>
      </svg>
    </div>
  );
}

// ─── Bot Status Badge ──────────────────────────────────────────────────────────
function BotStatus({ data }) {
  const now = new Date();
  const parseUTC = s => { try { return new Date(s); } catch { return null; } };
  const cbLong  = parseUTC(data.blocked_long_until);
  const cbShort = parseUTC(data.blocked_short_until);
  const cbUntil = [cbLong, cbShort].filter(d => d && d > now).sort((a,b)=>b-a)[0] || null;

  const hasPosition = data.positions
    ? Object.keys(data.positions).length > 0
    : (data.open_trades||[]).length > 0;

  if (cbUntil) {
    const mins = Math.round((cbUntil - now) / 60000);
    return <span style={{ background:"rgba(255,68,68,0.15)", border:"1px solid #ff444455", color:"#ff4444", borderRadius:6, padding:"3px 8px", fontSize:10, fontFamily:"monospace", fontWeight:700 }}>⛔ CB {mins}min</span>;
  }
  if (hasPosition) {
    return <span style={{ background:"rgba(0,255,136,0.1)", border:"1px solid #00ff8855", color:"#00ff88", borderRadius:6, padding:"3px 8px", fontSize:10, fontFamily:"monospace", fontWeight:700 }}>● OPERANDO</span>;
  }
  return <span style={{ background:"rgba(0,204,102,0.08)", border:"1px solid #00cc6633", color:"#00cc66", borderRadius:6, padding:"3px 8px", fontSize:10, fontFamily:"monospace" }}>● ACTIVO</span>;
}

// ─── Modal "Ver más" ──────────────────────────────────────────────────────────
function VerMasModal({ title, onClose, children }) {
  return (
    <div style={{ position:"fixed", inset:0, background:"rgba(0,0,0,0.85)", zIndex:1000, display:"flex", alignItems:"center", justifyContent:"center" }} onClick={onClose}>
      <div style={{ background:"#0e0e14", border:"1px solid rgba(255,255,255,0.1)", borderRadius:10, width:"min(92vw,720px)", maxHeight:"82vh", display:"flex", flexDirection:"column", padding:"20px 24px" }} onClick={e=>e.stopPropagation()}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:14, borderBottom:"1px solid rgba(255,255,255,0.06)", paddingBottom:12 }}>
          <span style={{ color:"#00ff88", fontWeight:700, letterSpacing:2, fontSize:11, fontFamily:"monospace" }}>{title}</span>
          <button onClick={onClose} style={{ background:"none", border:"none", color:"#888", cursor:"pointer", fontSize:20, lineHeight:1 }}>×</button>
        </div>
        <div style={{ overflowY:"auto", flex:1, paddingRight:4 }}>{children}</div>
      </div>
    </div>
  );
}

const verMasBtn = { background:"none", border:"1px solid rgba(255,255,255,0.08)", color:"#666", cursor:"pointer", fontSize:10, padding:"5px 0", borderRadius:5, marginTop:8, width:"100%", letterSpacing:1, fontFamily:"monospace" };

// Fecha local YYYY-MM-DD (usa hora Argentina, no UTC)
const localToday = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
};

// ─── Panel Altcoins ────────────────────────────────────────────────────────────
function AltcoinPanel({ data, liveprices, onClose }) {
  const [tab, setTab] = useState("positions");
  const [modal, setModal] = useState(null); // "log" | "trades" | null
  const T = (id, label) => (
    <button onClick={()=>setTab(id)} style={{ background:tab===id?"rgba(255,100,200,0.1)":"transparent", border:`1px solid ${tab===id?"#ff64c855":"transparent"}`, color:tab===id?"#ff64c8":"#bbb", borderRadius:7, padding:"5px 14px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>{label}</button>
  );
  const open = data.open_positions || [];
  const closed = data.closed_trades || [];
  const wins = closed.filter(t=>t.pnl>0);
  const scan = data.last_scan || [];
  const scanning = data.scanning || false;
  const stratColor = (s) => s==="MEAN_REVERSION"?"#00ff88":s==="MOMENTUM"?"#ffb800":s==="RANGE"?"#888bff":"#bbb";

  return (
    <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:16, padding:22, minWidth:0, overflow:"hidden" }}>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
        <div>
          <div style={{ color:"#ff64c8", fontWeight:700, letterSpacing:2, fontSize:13 }}>ALTCOINS ADAPTIVO</div>
          <div style={{ color:"#bbb", fontSize:11 }}>Top 20 por volumen · scoring técnico · paper trading</div>
        </div>
        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
          {(() => {
            const now = new Date();
            const cooldowns = data.cooldowns || {};
            const activeCooldowns = Object.entries(cooldowns).filter(([,t]) => new Date(t) > now);
            if (scanning) return <span style={{ background:"rgba(255,100,200,0.15)", border:"1px solid #ff64c855", color:"#ff64c8", borderRadius:6, padding:"3px 8px", fontSize:10, fontFamily:"monospace", fontWeight:700 }}>🔍 ESCANEANDO</span>;
            if (open.length > 0) return <span style={{ background:"rgba(0,255,136,0.1)", border:"1px solid #00ff8855", color:"#00ff88", borderRadius:6, padding:"3px 8px", fontSize:10, fontFamily:"monospace", fontWeight:700 }}>● OPERANDO ({open.length})</span>;
            if (activeCooldowns.length > 0) return <span style={{ background:"rgba(255,184,0,0.1)", border:"1px solid #ffb80055", color:"#ffb800", borderRadius:6, padding:"3px 8px", fontSize:10, fontFamily:"monospace" }}>⏳ CD ({activeCooldowns.length} sym)</span>;
            return <span style={{ background:"rgba(0,204,102,0.08)", border:"1px solid #00cc6633", color:"#00cc66", borderRadius:6, padding:"3px 8px", fontSize:10, fontFamily:"monospace" }}>● ACTIVO</span>;
          })()}
          <Badge text="PAPER" color="#ff64c8" />
        </div>
      </div>

      <div className="stat-row" style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)", flexWrap:"wrap", gap:12 }}>
        <Stat label="Capital" value={`$${(data.current_capital||0).toFixed(2)}`} />
        <PnlDisplay pnl={data.total_pnl||0} pct={data.total_pnl_pct||0} />
        <Stat label="Win Rate" value={`${(data.win_rate||0).toFixed(0)}%`} color="#ffcc00" />
        <Stat label="Abiertas" value={open.length} color="#ff64c8" size={20} />
        <Stat label="Trades" value={data.total_trades||0} color="#bbb" size={20} />
      </div>

      <div style={{ display:"flex", gap:6, marginBottom:14 }}>
        {T("positions","POSICIONES")}
        {T("scanner", scanning?"SCANNER ●":"SCANNER")}
        {T("trades","TRADES")}
        {T("stats","STATS")}
        {T("log","LOG")}
      </div>

      {tab==="positions" && (
        <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
          {open.length===0
            ? <div style={{ color:"#ccc", textAlign:"center", padding:24 }}>Sin posiciones abiertas — escaneando mercado</div>
            : open.map((p,i)=>(
              <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.06)", borderRadius:10, padding:"12px 16px" }}>
                <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:6 }}>
                  <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                    <a href={`https://www.binance.com/en/futures/${p.symbol}`} target="_blank" rel="noopener noreferrer" style={{ color:"#ff64c8", fontWeight:700, fontFamily:"monospace", textDecoration:"none" }}>{p.symbol}</a>
                    <Badge text={p.direction||p.side} color={(p.direction||p.side)==="LONG"?"#00ff88":"#ff4444"} />
                    <Badge text={p.strategy} color={stratColor(p.strategy)} />
                    <Badge text={p.confidence} color={p.confidence==="HIGH"?"#00ff88":"#ffcc00"} />
                  </div>
                  <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:4 }}>
                    <div style={{ display:"flex", gap:10, alignItems:"center" }}>
                      <span style={{ color:"#ffb800", fontFamily:"monospace" }}>${(p.size_usdt||p.size||0).toFixed(0)}</span>
                      {liveprices?.[p.symbol] && (
                        <span style={{ color:"#bbb", fontFamily:"monospace", fontSize:11 }}>${liveprices[p.symbol]?.toFixed(4)}</span>
                      )}
                      {onClose && (
                        <button onClick={()=>onClose(p)} style={{ background:"rgba(255,68,68,0.15)", border:"1px solid #ff444455", color:"#ff6666", borderRadius:6, padding:"4px 10px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>
                          CERRAR
                        </button>
                      )}
                    </div>
                    {(() => {
                      const live = liveprices?.[p.symbol];
                      if (!live || !p.entry_price) return null;
                      const side = p.direction || p.side || "LONG";
                      const size = p.size_usdt || p.size || 0;
                      const lev = p.leverage || 1;
                      const pnlPct = side === "LONG"
                        ? (live - p.entry_price) / p.entry_price * lev
                        : (p.entry_price - live) / p.entry_price * lev;
                      const pnlUsd = size * pnlPct;
                      const col = pnlUsd >= 0 ? "#00ff88" : "#ff4444";
                      return (
                        <div style={{ textAlign:"right" }}>
                          <div style={{ color:col, fontWeight:700, fontSize:16, fontFamily:"monospace" }}>
                            {pnlUsd >= 0 ? "+" : ""}{pnlUsd.toFixed(2)}$
                          </div>
                          <div style={{ color:col+"aa", fontWeight:700, fontSize:12, fontFamily:"monospace" }}>
                            {pnlPct >= 0 ? "+" : ""}{(pnlPct*100).toFixed(2)}%
                          </div>
                        </div>
                      );
                    })()}
                  </div>
                </div>
                <div style={{ color:"#bbb", fontSize:11 }}>{p.reasoning?.slice(0,80)}</div>
                <div style={{ display:"flex", gap:14, marginTop:6, fontFamily:"monospace", fontSize:11, flexWrap:"wrap" }}>
                  <span style={{ color:"#bbb" }}>entrada <span style={{ color:"#ccc" }}>${p.entry_price?.toFixed(4)}</span></span>
                  <span style={{ color:"#bbb" }}>SL <span style={{ color:"#ff4444" }}>${p.stop_loss?.toFixed(4)}</span></span>
                  <span style={{ color:"#bbb" }}>TP <span style={{ color:"#00ff88" }}>${p.take_profit?.toFixed(4)}</span></span>
                  {p.entry_time && <span style={{ color:"#ccc" }}>{new Date(p.entry_time).toLocaleString("es-AR",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})}</span>}
                </div>
              </div>
            ))
          }
          {/* Cooldowns activos */}
          {(() => {
            const now = new Date();
            const cds = Object.entries(data.cooldowns || {})
              .filter(([, t]) => new Date(t) > now)
              .sort((a, b) => new Date(a[1]) - new Date(b[1]));
            if (!cds.length) return null;
            return (
              <div style={{ marginTop:12, borderTop:"1px solid rgba(255,255,255,0.05)", paddingTop:10 }}>
                <div style={{ color:"#ffb800", fontSize:10, letterSpacing:1, marginBottom:6, textTransform:"uppercase" }}>
                  Cooldowns ({cds.length})
                </div>
                <div style={{ display:"flex", flexWrap:"wrap", gap:6 }}>
                  {cds.map(([sym, exp]) => {
                    const mins = Math.max(0, Math.ceil((new Date(exp) - now) / 60000));
                    return (
                      <span key={sym} style={{ background:"rgba(255,184,0,0.08)", border:"1px solid #ffb80033",
                        color:"#ffb800", borderRadius:6, padding:"2px 8px", fontSize:10, fontFamily:"monospace" }}>
                        {sym.replace("USDT","")} {mins}m
                      </span>
                    );
                  })}
                </div>
              </div>
            );
          })()}
        </div>
      )}

      {tab==="scanner" && (
        <div>
          <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:12 }}>
            {scanning && <div style={{ width:8, height:8, borderRadius:"50%", background:"#ff64c8" }} />}
            <span style={{ color:"#bbb", fontSize:11 }}>{scanning ? "Escaneando mercado..." : `Último scan: ${scan.length} altcoins analizadas`}</span>
          </div>
          <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
            {scan.length===0
              ? <div style={{ color:"#ccc", textAlign:"center", padding:24 }}>Esperando primer scan...</div>
              : scan.map((s,i)=>{
                  const skipIt = s.strategy==="SKIP" || s.direction==="SKIP" || s.confidence==="LOW";
                  return (
                    <div key={i} style={{ background:skipIt?"rgba(255,255,255,0.01)":"rgba(255,255,255,0.03)", border:`1px solid ${skipIt?"rgba(255,255,255,0.04)":"rgba(255,255,255,0.08)"}`, borderRadius:8, padding:"10px 14px", opacity:skipIt?0.5:1 }}>
                      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:skipIt?0:6 }}>
                        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                          <span style={{ color:"#ff64c8", fontFamily:"monospace", fontWeight:700, minWidth:90 }}>{s.symbol}</span>
                          <Badge text={`RSI ${s.rsi?.toFixed(0)}`} color={s.rsi<30?"#00ff88":s.rsi>70?"#ff4444":"#bbb"} />
                          <Badge text={`Vol ${s.vol_ratio?.toFixed(1)}x`} color={s.vol_ratio>2?"#ffcc00":"#bbb"} />
                        </div>
                        <div style={{ display:"flex", gap:6 }}>
                          {!skipIt && <Badge text={s.strategy} color={stratColor(s.strategy)} />}
                          {!skipIt && <Badge text={s.direction||s.side} color={(s.direction||s.side)==="LONG"?"#00ff88":"#ff4444"} />}
                          {!skipIt && <Badge text={s.confidence} color={s.confidence==="HIGH"?"#00ff88":"#ffcc00"} />}
                          {skipIt && <Badge text="SKIP" color="#333" />}
                          {!skipIt && (s.size_usdt||0)>0 && <span style={{ color:"#ffb800", fontSize:11, fontFamily:"monospace" }}>${(s.size_usdt||0).toFixed(0)}</span>}
                        </div>
                      </div>
                      {!skipIt && s.reasoning && <div style={{ color:"#bbb", fontSize:11 }}>"{s.reasoning?.slice(0,100)}"</div>}
                    </div>
                  );
                })
            }
          </div>
        </div>
      )}

      {tab==="trades" && (() => {
        const today = localToday();
        const todayTrades = [...closed].filter(t=>(t.exit_time||t.entry_time||"").startsWith(today)).reverse();
        const AltTradeRow = ({t,i}) => (
          <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"10px 14px", marginBottom:6 }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center" }}>
              <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                <a href={`https://www.binance.com/en/futures/${t.symbol}`} target="_blank" rel="noopener noreferrer" style={{ color:"#ff64c8", fontFamily:"monospace", minWidth:80, textDecoration:"none" }}>{t.symbol}</a>
                <Badge text={t.direction||t.side} color={(t.direction||t.side)==="LONG"?"#00ff88":"#ff4444"} />
                <Badge text={t.strategy} color={stratColor(t.strategy)} />
                <Badge text={t.exit_reason||"CLOSE"} color={t.exit_reason==="TAKE_PROFIT"?"#00ff88":t.exit_reason==="STOP_LOSS"?"#ff4444":"#bbb"} />
              </div>
              <div style={{ textAlign:"right" }}>
                <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}</span>
                {(t.size_usdt||t.size)>0 && <span style={{ color:"#888", fontFamily:"monospace", fontSize:11, marginLeft:8 }}>${(t.size_usdt||t.size||0).toFixed(0)}</span>}
              </div>
            </div>
            <div style={{ display:"flex", gap:14, marginTop:6, fontFamily:"monospace", fontSize:11, flexWrap:"wrap" }}>
              {t.entry_price>0 && <span style={{ color:"#bbb" }}>entrada <span style={{ color:"#ccc" }}>${t.entry_price>100?t.entry_price.toFixed(2):t.entry_price.toFixed(4)}</span></span>}
              {t.exit_price>0 && <span style={{ color:"#bbb" }}>salida <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444" }}>${t.exit_price>100?t.exit_price.toFixed(2):t.exit_price.toFixed(4)}</span></span>}
              {t.pnl_pct!=null && <span style={{ color:t.pnl>=0?"#00ff88aa":"#ff4444aa" }}>{t.pnl_pct>=0?"+":""}{t.pnl_pct.toFixed(1)}%</span>}
            </div>
            {(t.exit_time||t.entry_time) && <div style={{ color:"#888", fontSize:10, fontFamily:"monospace", marginTop:4 }}>{new Date(t.exit_time||t.entry_time).toLocaleString("es-AR",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"})} — {t.entry_time ? `${((new Date(t.exit_time||t.entry_time)-new Date(t.entry_time))/60000).toFixed(0)}min` : ""}</div>}
          </div>
        );
        return (
          <div style={{ display:"flex", flexDirection:"column" }}>
            {todayTrades.length===0
              ? <div style={{ color:"#bbb", textAlign:"center", padding:24 }}>Sin trades hoy</div>
              : todayTrades.slice(0,15).map((t,i)=><AltTradeRow key={i} t={t} i={i}/>)
            }
            {todayTrades.length>0 && (
              <button style={verMasBtn} onClick={()=>setModal("trades")}>
                Ver más... ({todayTrades.length} trades hoy)
              </button>
            )}
            {modal==="trades" && (
              <VerMasModal title={`ALTCOIN — TRADES HOY (${todayTrades.length})`} onClose={()=>setModal(null)}>
                {todayTrades.map((t,i)=><AltTradeRow key={i} t={t} i={i}/>)}
              </VerMasModal>
            )}
          </div>
        );
      })()}

      {tab==="stats" && (
        <div style={{ display:"grid", gridTemplateColumns:"repeat(4, 1fr)", gap:10 }}>
          {[
            { label:"Capital inicial", value:`$${(data.initial_capital||0).toFixed(2)}` },
            { label:"Capital actual", value:`$${(data.current_capital||0).toFixed(2)}` },
            { label:"Retorno", value:`${(data.total_pnl_pct||0)>=0?"+":""}${(data.total_pnl_pct||0).toFixed(2)}%`, color:(data.total_pnl_pct||0)>=0?"#00ff88":"#ff4444" },
            { label:"Win rate", value:`${(data.win_rate||0).toFixed(1)}%`, color:"#ffcc00" },
            { label:"Total trades", value:data.total_trades||0 },
            { label:"Ganadores", value:wins.length, color:"#00ff88" },
            { label:"Perdedores", value:(data.total_trades||0)-wins.length, color:"#ff4444" },
            { label:"Mejor trade", value:closed.length?`$${Math.max(...closed.map(t=>t.pnl||0)).toFixed(2)}`:"--", color:"#00ff88" },
          ].map((s,i)=>(
            <div key={i} style={{ background:"rgba(255,255,255,0.02)", borderRadius:8, padding:"10px 14px" }}>
              <div style={{ color:"#bbb", fontSize:10, marginBottom:4 }}>{s.label}</div>
              <div style={{ color:s.color||"#ccc", fontFamily:"monospace", fontWeight:700 }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      {tab==="log" && (() => {
        const log = data.cycle_log||[];
        const LogRow = ({e,i}) => (
          <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
            <span style={{ color:"#bbb", minWidth:50 }}>{e.time}</span>
            <span style={{ color:e.msg?.includes("✓")?"#00ff88":e.msg?.includes("❌")?"#ff4444":"#bbb" }}>{e.msg}</span>
          </div>
        );
        return (
          <div>
            <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
              {log.slice(0,15).map((e,i)=><LogRow key={i} e={e} i={i}/>)}
            </div>
            {log.length>0 && (
              <button style={verMasBtn} onClick={()=>setModal("log")}>
                Ver más... ({log.length} entradas)
              </button>
            )}
            {modal==="log" && (
              <VerMasModal title={`ALTCOIN — LOG HOY (${log.length})`} onClose={()=>setModal(null)}>
                <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
                  {log.map((e,i)=><LogRow key={i} e={e} i={i}/>)}
                </div>
              </VerMasModal>
            )}
          </div>
        );
      })()}
    </div>
  );
}
// ─── Panel AltScalp (HFT Altcoins) ───────────────────────────────────────────
function AltScalpPanel({ data, liveprices, onClose }) {
  const [tab, setTab] = useState("positions");
  const [modal, setModal] = useState(null);
  const ACC = "#00ccff";
  const T = (id, label) => (
    <button onClick={()=>setTab(id)} style={{ background:tab===id?`rgba(0,204,255,0.1)`:"transparent", border:`1px solid ${tab===id?"#00ccff55":"transparent"}`, color:tab===id?ACC:"#bbb", borderRadius:7, padding:"5px 14px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>{label}</button>
  );
  const positions = Object.values(data.positions||{});
  const closed    = data.closed_trades||[];

  const Badge2 = ({text, color}) => (
    <span style={{ background:`${color}22`, border:`1px solid ${color}44`, color, borderRadius:4, padding:"2px 7px", fontSize:10, fontFamily:"monospace" }}>{text}</span>
  );

  return (
    <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(0,204,255,0.15)", borderRadius:12, padding:"18px 20px" }}>
      {/* Header */}
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
        <div>
          <div style={{ color:ACC, fontWeight:700, letterSpacing:2, fontSize:13 }}>⚡ ALTSCALP HFT</div>
          <div style={{ color:"#bbb", fontSize:11 }}>Alta frecuencia · 15s · leverage 10/20/50x · paper trading</div>
        </div>
        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
          {positions.length>0
            ? <span style={{ background:"rgba(0,204,255,0.1)", border:"1px solid #00ccff55", color:ACC, borderRadius:6, padding:"3px 8px", fontSize:10, fontFamily:"monospace", fontWeight:700 }}>● OPERANDO ({positions.length})</span>
            : <span style={{ background:"rgba(0,204,102,0.08)", border:"1px solid #00cc6633", color:"#00cc66", borderRadius:6, padding:"3px 8px", fontSize:10, fontFamily:"monospace" }}>● ACTIVO</span>
          }
          <Badge text="PAPER" color={ACC} />
        </div>
      </div>

      {/* Stats bar */}
      {(() => {
        const effectiveCap = getEquity(data, liveprices);
        return (
          <div className="stat-row" style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)", flexWrap:"wrap", gap:12 }}>
            <Stat label="Capital"   value={`$${effectiveCap.toFixed(2)}`} />
            <PnlDisplay pnl={data.total_pnl||0} pct={data.total_pnl_pct||0} />
            <Stat label="Win Rate"  value={`${(data.win_rate||0).toFixed(0)}%`} color="#ffcc00" />
            <Stat label="Abiertas"  value={positions.length} color={ACC} size={20} />
            <Stat label="Trades"    value={(data.closed_trades||[]).length} color="#bbb" size={20} />
            <Stat label="Max DD"    value={`${(data.max_drawdown||0).toFixed(1)}%`} color="#ff8c00" size={16} />
          </div>
        );
      })()}

      {/* Tabs */}
      <div style={{ display:"flex", gap:6, marginBottom:14, flexWrap:"wrap" }}>
        {T("positions","POSICIONES")} {T("scanner","SCANNER")} {T("trades","TRADES")} {T("stats","STATS")} {T("log","LOG")}
      </div>

      {/* POSICIONES */}
      {tab==="positions" && (
        <div style={{ display:"flex", flexDirection:"column", gap:10 }}>
          {positions.length===0
            ? <div style={{ color:"#ccc", textAlign:"center", padding:32 }}>Sin posiciones abiertas — escaneando mercado</div>
            : positions.map((p,i)=>{
              const entry   = p.entry_price;
              const live    = liveprices?.[p.symbol];
              const curPx   = live || p.best_price || entry;
              const lev     = p.leverage || 1;
              const size    = p.size_usdt || 0;
              const pnlPct  = entry ? (p.direction==="LONG" ? (curPx-entry)/entry*lev : (entry-curPx)/entry*lev) : 0;
              const pnlUsd  = size * pnlPct;
              const col     = pnlUsd >= 0 ? "#00ff88" : "#ff4444";
              return (
                <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.08)", borderRadius:10, padding:"12px 16px" }}>
                  {/* Row 1: symbol+badges left, size+price+CERRAR right */}
                  <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:6 }}>
                    <div style={{ display:"flex", gap:8, alignItems:"center", flexWrap:"wrap" }}>
                      <a href={`https://www.binance.com/en/futures/${p.symbol}`} target="_blank" rel="noopener noreferrer"
                        style={{ color:ACC, fontWeight:700, fontFamily:"monospace", fontSize:13, textDecoration:"none" }}>{p.symbol}</a>
                      <Badge2 text={p.direction} color={p.direction==="LONG"?"#00ff88":"#ff4444"} />
                      <Badge2 text={`${lev}x`} color={ACC} />
                    </div>
                    <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:4 }}>
                      <div style={{ display:"flex", gap:10, alignItems:"center" }}>
                        <span style={{ color:"#ffb800", fontFamily:"monospace", fontWeight:700, fontSize:12 }}>${size.toFixed(0)}</span>
                        {curPx && <span style={{ color:"#bbb", fontFamily:"monospace", fontSize:11 }}>${curPx.toFixed(4)}</span>}
                        {onClose && (
                          <button onClick={()=>onClose(p)} style={{ background:"rgba(255,68,68,0.3)", border:"1px solid #ff4444bb", color:"#fff", borderRadius:5, padding:"4px 10px", fontSize:11, cursor:"pointer", fontFamily:"monospace", fontWeight:700, letterSpacing:1 }}>
                            CERRAR
                          </button>
                        )}
                      </div>
                      {entry && (
                        <div style={{ textAlign:"right" }}>
                          <div style={{ color:col, fontWeight:700, fontSize:16, fontFamily:"monospace", lineHeight:1 }}>
                            {pnlUsd >= 0 ? "+" : ""}{pnlUsd.toFixed(2)}$
                          </div>
                          <div style={{ color:col+"bb", fontWeight:700, fontSize:11, fontFamily:"monospace", marginTop:2 }}>
                            {pnlPct >= 0 ? "+" : ""}{(pnlPct*100).toFixed(2)}%
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                  {/* Row 2: score text */}
                  {p.score != null && (
                    <div style={{ color:"#888", fontFamily:"monospace", fontSize:10, marginBottom:4 }}>
                      Score {p.score >= 0 ? "+" : ""}{p.score}
                    </div>
                  )}
                  {/* Row 3: entrada / SL / TP / hora */}
                  <div style={{ display:"flex", gap:14, fontFamily:"monospace", fontSize:10, flexWrap:"wrap" }}>
                    <span style={{ color:"#bbb" }}>entrada <span style={{ color:"#ccc" }}>${entry?.toFixed(4)}</span></span>
                    <span style={{ color:"#bbb" }}>SL <span style={{ color:"#ff4444" }}>${p.stop_loss?.toFixed(4)}</span></span>
                    <span style={{ color:"#bbb" }}>TP <span style={{ color:"#00ff88" }}>${p.take_profit?.toFixed(4)}</span></span>
                    {p.entry_time && <span style={{ color:"#ccc" }}>{new Date(p.entry_time).toLocaleString("es-AR",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})}</span>}
                  </div>
                </div>
              );
            })
          }
        </div>
      )}

      {/* SCANNER */}
      {tab==="scanner" && (
        <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
          <div style={{ display:"grid", gridTemplateColumns:"1fr 80px 70px", gap:8, color:"#555", fontSize:9, letterSpacing:1, padding:"0 4px", marginBottom:4 }}>
            <span>SYMBOL</span><span style={{ textAlign:"right" }}>VOL 24H</span><span style={{ textAlign:"right" }}>CAMBIO</span>
          </div>
          {(data.scanner_coins||[]).length===0
            ? <div style={{ color:"#bbb", textAlign:"center", padding:24, fontSize:12 }}>Sin datos de scanner</div>
            : (data.scanner_coins||[]).map((c,i)=>(
              <div key={i} style={{ display:"grid", gridTemplateColumns:"1fr 80px 70px", gap:8, background:"rgba(255,255,255,0.02)", borderRadius:6, padding:"7px 10px", alignItems:"center" }}>
                <span style={{ color:ACC, fontFamily:"monospace", fontSize:11 }}>{c.symbol}</span>
                <span style={{ color:"#ccc", fontFamily:"monospace", fontSize:10, textAlign:"right" }}>${c.volume}M</span>
                <span style={{ color:c.change_pct>3?"#00ff88":c.change_pct>1?"#ffcc00":"#bbb", fontFamily:"monospace", fontSize:10, textAlign:"right" }}>{c.change_pct?.toFixed?.(1)}%</span>
              </div>
            ))
          }
        </div>
      )}

      {/* TRADES */}
      {tab==="trades" && (()=>{
        const today = localToday();
        const todayTrades = [...closed].filter(t=>(t.exit_time||t.entry_time||"").startsWith(today)).reverse();
        const Row = ({t,i}) => (
          <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"8px 12px", marginBottom:4 }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center" }}>
              <div style={{ display:"flex", gap:6, alignItems:"center", flexWrap:"wrap" }}>
                <Badge2 text={t.side} color={t.side==="LONG"?"#00ff88":"#ff4444"} />
                <Badge2 text={t.exit_reason||"--"} color={t.exit_reason==="TAKE_PROFIT"?"#00ff88":t.exit_reason==="STOP_LOSS"?"#ff4444":"#bbb"} />
                <span style={{ color:ACC, fontSize:11, fontWeight:700 }}>{t.symbol}</span>
                <span style={{ color:"#888", fontSize:10 }}>{t.leverage}x</span>
              </div>
              <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}</span>
            </div>
            <div style={{ display:"flex", gap:10, fontSize:10, color:"#666", fontFamily:"monospace", marginTop:3 }}>
              {(t.exit_time||t.entry_time) && <span>{(t.exit_time||t.entry_time).slice(11,16)}</span>}
              <span>${t.entry_price?.toFixed?.(4)} → ${t.exit_price?.toFixed?.(4)}</span>
            </div>
          </div>
        );
        return (
          <div>
            {todayTrades.length===0
              ? <div style={{ color:"#bbb", textAlign:"center", padding:24 }}>Sin trades hoy</div>
              : todayTrades.slice(0,15).map((t,i)=><Row key={i} t={t} i={i}/>)
            }
            {todayTrades.length>0 && (
              <button style={verMasBtn} onClick={()=>setModal("trades")}>
                Ver más... ({todayTrades.length} trades hoy)
              </button>
            )}
            {modal==="trades" && (
              <VerMasModal title={`ALTSCALP — TRADES HOY (${todayTrades.length})`} onClose={()=>setModal(null)}>
                <div>{todayTrades.map((t,i)=><Row key={i} t={t} i={i}/>)}</div>
              </VerMasModal>
            )}
          </div>
        );
      })()}

      {/* STATS */}
      {tab==="stats" && (
        <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:12 }}>
          {[
            { label:"Capital inicial", value:`$${(data.initial_capital||0).toFixed(2)}` },
            { label:"Capital actual",  value:`$${(data.current_capital||0).toFixed(2)}` },
            { label:"P&L total",       value:`${(data.total_pnl||0)>=0?"+":""}$${Math.abs(data.total_pnl||0).toFixed(2)}`, color:(data.total_pnl||0)>=0?"#00ff88":"#ff4444" },
            { label:"Win rate",        value:`${(data.win_rate||0).toFixed(1)}%`, color:"#ffcc00" },
            { label:"Max drawdown",    value:`${(data.max_drawdown||0).toFixed(1)}%`, color:"#ff8c00" },
            { label:"Trades totales",  value:(data.closed_trades||[]).length },
          ].map((s,i)=>(
            <div key={i} style={{ background:"rgba(255,255,255,0.02)", borderRadius:8, padding:"10px 14px" }}>
              <div style={{ color:"#bbb", fontSize:10, marginBottom:4 }}>{s.label}</div>
              <div style={{ color:s.color||"#ccc", fontFamily:"monospace", fontWeight:700 }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      {/* LOG */}
      {tab==="log" && (()=>{
        const log2 = data.cycle_log||[];
        const LogRow = ({e,i}) => (
          <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
            <span style={{ color:"#bbb", minWidth:50 }}>{e.time}</span>
            <span style={{ color:e.msg?.includes("✅")?"#00ff88":e.msg?.includes("❌")?"#ff4444":e.msg?.includes("↗")||e.msg?.includes("AS-")?ACC:"#bbb" }}>{e.msg}</span>
          </div>
        );
        return (
          <div>
            <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
              {log2.slice(0,20).map((e,i)=><LogRow key={i} e={e} i={i}/>)}
            </div>
            {log2.length>0 && (
              <button style={verMasBtn} onClick={()=>setModal("log")}>
                Ver más... ({log2.length} entradas)
              </button>
            )}
            {modal==="log" && (
              <VerMasModal title={`ALTSCALP — LOG (${log2.length})`} onClose={()=>setModal(null)}>
                <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
                  {log2.map((e,i)=><LogRow key={i} e={e} i={i}/>)}
                </div>
              </VerMasModal>
            )}
          </div>
        );
      })()}
    </div>
  );
}

// ─── Panel Scalping ───────────────────────────────────────────────────────────
function ScalpingPanel({ data, liveprices, onClose }) {
  const [tab, setTab] = useState("position");
  const [modal, setModal] = useState(null); // "log" | "trades" | null
  const [btcScan, setBtcScan] = useState(null);
  const ACC = "#ff9933";
  const T = (id, label) => (
    <button onClick={()=>setTab(id)} style={{ background:tab===id?`rgba(255,153,51,0.1)`:"transparent", border:`1px solid ${tab===id?"#ff993355":"transparent"}`, color:tab===id?ACC:"#bbb", borderRadius:7, padding:"5px 14px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>{label}</button>
  );

  const openTrades = [
    ...Object.values(data.positions || {}),
    ...(data.open_trades || []),
  ].filter((t, i, arr) => arr.findIndex(x => (x.id && x.id === t.id) || (x.symbol && x.symbol === t.symbol)) === i);
  const closed    = data.all_closed_trades || (data.closed_trades || []);
  const btcLive   = liveprices?.["BTCUSDT"] || data.btc_price || 0;

  // Análisis técnico en vivo (1m)
  useEffect(() => {
    const load = async () => {
      try {
        const r = await fetch("https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1m&limit=100");
        const klines = await r.json();
        if (!klines.length) return;
        const closes = klines.map(k=>parseFloat(k[4]));
        const vols   = klines.map(k=>parseFloat(k[5]));
        const diffs  = closes.slice(1).map((c,i)=>c-closes[i]);
        const gains  = diffs.map(d=>d>0?d:0), losses = diffs.map(d=>d<0?-d:0);
        const avgGain = gains.slice(-14).reduce((a,b)=>a+b,0)/14;
        const avgLoss = losses.slice(-14).reduce((a,b)=>a+b,0)/14;
        const rsi  = avgLoss===0?100:100-(100/(1+avgGain/avgLoss));
        const rsiV = rsi<30?"OVERSOLD":rsi>70?"OVERBOUGHT":"NEUTRAL";
        const sma20 = closes.slice(-20).reduce((a,b)=>a+b,0)/20;
        const std20 = Math.sqrt(closes.slice(-20).map(c=>(c-sma20)**2).reduce((a,b)=>a+b,0)/20);
        const bbPct = (closes[closes.length-1]-(sma20-2*std20))/(4*std20);
        const bbV   = bbPct<0.2?"LOWER":bbPct>0.8?"UPPER":"MIDDLE";
        const ema   = (arr,span) => arr.reduce((acc,v,i)=>i===0?[v]:[...acc,v*(2/(span+1))+acc[i-1]*(1-2/(span+1))],[]);
        const ema12 = ema(closes,12); const ema26 = ema(closes,26);
        const macd  = ema12.map((v,i)=>v-ema26[i]);
        const sig   = ema(macd,9);
        const hist  = macd[macd.length-1]-sig[sig.length-1];
        const prev  = macd[macd.length-2]-sig[sig.length-2];
        const macdCross = hist>0&&prev<=0?"bullish":hist<0&&prev>=0?"bearish":"neutral";
        const avgVol = vols.slice(-20).reduce((a,b)=>a+b,0)/20;
        const volRatio = vols[vols.length-1]/avgVol;
        const ema50 = ema(closes,50); const ema200 = ema(closes,200);
        const trend = ema50[ema50.length-1]>ema200[ema200.length-1]?"bullish":"bearish";
        let score=0, signals=[];
        if(rsi<30){score+=3;signals.push("RSI oversold")} else if(rsi>70){score-=3;signals.push("RSI overbought")}
        if(bbV==="LOWER"){score+=2;signals.push("BB lower")} else if(bbV==="UPPER"){score-=2;signals.push("BB upper")}
        if(macdCross==="bullish"){score+=2;signals.push("MACD bullish")} else if(macdCross==="bearish"){score-=2;signals.push("MACD bearish")}
        if(trend==="bullish")score+=1; else score-=1;
        if(volRatio>2){score=Math.round(score*1.5);signals.push(`Vol ${volRatio.toFixed(1)}x`)}
        setBtcScan({rsi:rsiV, bb:bbV, macdCross, trend, volRatio:parseFloat(volRatio.toFixed(2)), score, signals, price:closes[closes.length-1]});
      } catch { /* ignore transient market-data errors */ }
    };
    load();
    const t = setInterval(load, 15000);
    return ()=>clearInterval(t);
  }, []);

  return (
    <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,153,51,0.2)", borderRadius:16, padding:22, minWidth:0, overflow:"hidden" }}>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
        <div>
          <div style={{ color:ACC, fontWeight:700, letterSpacing:2, fontSize:13 }}>SCALPING BTC</div>
          <div style={{ color:"#bbb", fontSize:11 }}>BTC/USDT · {data.leverage||10}x · paper trading</div>
        </div>
        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
          {btcLive > 0 && <span style={{ color:ACC, fontFamily:"monospace", fontWeight:700 }}>${btcLive.toLocaleString()}</span>}
          <BotStatus data={data} />
          <Badge text="PAPER" color={ACC} />
        </div>
      </div>

      {(() => {
        const effectiveCap = getEquity(data, liveprices);
        return (
          <div className="stat-row" style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)", flexWrap:"wrap", gap:12 }}>
            <Stat label="Capital"  value={`$${effectiveCap.toFixed(2)}`} />
            <PnlDisplay pnl={data.total_pnl||0} pct={data.total_pnl_pct||0} />
            <Stat label="RSI 1m"   value={data.rsi?.toFixed(1)||"--"} color={data.rsi<30?"#00ff88":data.rsi>70?"#ff4444":"#ccc"} />
            <Stat label="Win rate" value={`${(data.win_rate||0).toFixed(0)}%`} color="#ffcc00" size={18} />
          </div>
        );
      })()}

      {openTrades.map((openTrade, idx) => {
        const posColor = openTrade.side==="LONG" ? "#00ff88" : openTrade.side==="SHORT" ? "#ff4444" : "#bbb";
        const unrealizedRaw = btcLive
          ? (openTrade.side==="LONG"
              ? (btcLive - openTrade.entry_price) / openTrade.entry_price
              : (openTrade.entry_price - btcLive) / openTrade.entry_price
            ) * (openTrade.leverage || 5)
          : null;
        const unrealizedPct = unrealizedRaw !== null ? (unrealizedRaw * 100).toFixed(2) : null;
        const unrealizedUsd = unrealizedRaw !== null ? (unrealizedRaw * (openTrade.size || 0)) : null;
        return (
          <div key={openTrade.id || openTrade.symbol || idx} style={{ background:`${posColor}11`, border:`1px solid ${posColor}33`, borderRadius:10, padding:"12px 16px", marginBottom:8 }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:6 }}>
              <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                <Badge text={openTrade.side} color={posColor} />
                <span style={{ color:"#bbb", fontSize:12 }}>entrada ${openTrade.entry_price?.toLocaleString()}</span>
                <Badge text={openTrade.confidence||"--"} color={openTrade.confidence==="HIGH"?"#00ff88":"#ffcc00"} />
              </div>
              <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:4 }}>
                {onClose && (
                  <button onClick={()=>onClose(openTrade)} style={{ background:"rgba(255,68,68,0.15)", border:"1px solid #ff444455", color:"#ff6666", borderRadius:6, padding:"4px 10px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>
                    CERRAR
                  </button>
                )}
                {unrealizedPct && (() => {
                  const col = parseFloat(unrealizedPct) >= 0 ? "#00ff88" : "#ff4444";
                  const sign = parseFloat(unrealizedPct) >= 0 ? "+" : "";
                  return (
                    <div style={{ textAlign:"right" }}>
                      <div style={{ color:col, fontWeight:700, fontSize:16, fontFamily:"monospace" }}>{sign}{unrealizedUsd.toFixed(2)}$</div>
                      <div style={{ color:col+"aa", fontWeight:700, fontSize:12, fontFamily:"monospace" }}>{sign}{unrealizedPct}%</div>
                    </div>
                  );
                })()}
              </div>
            </div>
            <div style={{ display:"flex", gap:14, fontFamily:"monospace", fontSize:11 }}>
              <span style={{ color:"#bbb" }}>size <span style={{ color:ACC }}>${openTrade.size?.toFixed(2)}</span></span>
              <span style={{ color:"#bbb" }}>SL <span style={{ color:"#ff4444" }}>${openTrade.stop_loss?.toLocaleString()}</span></span>
              <span style={{ color:"#bbb" }}>TP <span style={{ color:"#00ff88" }}>${openTrade.take_profit?.toLocaleString()}</span></span>
            </div>
            {openTrade.reasoning && <div style={{ color:"#bbb", fontSize:11, marginTop:6 }}>{openTrade.reasoning?.slice(0,80)}...</div>}
          </div>
        );
      })}

      <div style={{ display:"flex", gap:6, marginBottom:14, flexWrap:"wrap" }}>
        <Badge text={`Trend: ${data.trend||"--"}`} color={data.trend==="bullish"?"#00ff88":data.trend==="bearish"?"#ff4444":"#bbb"} />
        <Badge text={`MACD: ${data.macd_cross||"--"}`} color={data.macd_cross==="bullish"?"#00ff88":data.macd_cross==="bearish"?"#ff4444":"#bbb"} />
        <Badge text={`Vol: ${data.vol_ratio?.toFixed(1)||"--"}x`} color={data.vol_ratio>1.5?"#ffcc00":"#bbb"} />
        <Badge text={`Regime: ${data.regime||"--"}`} color={data.regime==="TREND"?ACC:"#bbb"} />
      </div>

      {btcScan && (
        <div style={{ background:`rgba(255,153,51,0.04)`, border:`1px solid rgba(255,153,51,0.12)`, borderRadius:8, padding:"10px 14px", marginBottom:14 }}>
          <div style={{ display:"flex", gap:6, flexWrap:"wrap", alignItems:"center" }}>
            <Badge text={`RSI: ${btcScan.rsi}`} color={btcScan.rsi==="OVERSOLD"?"#00ff88":btcScan.rsi==="OVERBOUGHT"?"#ff4444":"#bbb"} />
            <Badge text={`BB: ${btcScan.bb}`} color={btcScan.bb==="LOWER"?"#00ff88":btcScan.bb==="UPPER"?"#ff4444":"#bbb"} />
            <Badge text={`MACD: ${btcScan.macdCross}`} color={btcScan.macdCross==="bullish"?"#00ff88":btcScan.macdCross==="bearish"?"#ff4444":"#bbb"} />
            <Badge text={btcScan.trend} color={btcScan.trend==="bullish"?"#00ff88":"#ff4444"} />
            <Badge text={`Vol ${btcScan.volRatio}x`} color={btcScan.volRatio>2?"#ffcc00":"#bbb"} />
            <span style={{ color:btcScan.score>=3?"#00ff88":btcScan.score<=-3?"#ff4444":"#bbb", fontFamily:"monospace", fontWeight:700, fontSize:12, marginLeft:4 }}>
              Score: {btcScan.score>=0?"+":""}{btcScan.score}
            </span>
          </div>
        </div>
      )}

      <div style={{ display:"flex", gap:6, marginBottom:14 }}>{T("position","POSICIÓN")}{T("trades","TRADES")}{T("stats","STATS")}{T("log","LOG")}</div>

      {tab==="position" && (
        <div>
          <BTCChart entryPrice={openTrades[0]?.entry_price} side={openTrades[0]?.side} stopLoss={openTrades[0]?.stop_loss} defaultInterval="1m" />
          {openTrades.length === 0 && (
            <div style={{ color:"#ccc", textAlign:"center", padding:12, fontSize:12 }}>Sin posición — esperando señal scalping</div>
          )}
          {openTrades.length > 0 && (
            <div style={{ color:"#bbb", fontSize:12, textAlign:"center" }}>
              {openTrades.length === 1
                ? `Trade activo desde ${openTrades[0].entry_time?.slice(0,16)?.replace("T"," ")}`
                : `${openTrades.length} trades activos`
              }
            </div>
          )}
        </div>
      )}

      {tab==="trades" && (()=>{
        const today = localToday();
        const todayTrades = [...closed].filter(t=>(t.exit_time||t.entry_time||"").startsWith(today)).reverse();
        const ScTradeRow = ({t,i}) => (
          <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"10px 14px" }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:4 }}>
              <div style={{ display:"flex", gap:8, alignItems:"center", flexWrap:"wrap" }}>
                <Badge text={t.side} color={t.side==="LONG"?"#00ff88":"#ff4444"} />
                <Badge text={t.exit_reason||"--"} color={t.exit_reason==="TAKE_PROFIT"?"#00ff88":t.exit_reason==="STOP_LOSS"?"#ff4444":"#bbb"} />
                <span style={{ color:"#bbb", fontSize:11 }}>${t.entry_price?.toLocaleString()} → ${t.exit_price?.toLocaleString()}</span>
              </div>
              <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}</span>
            </div>
            <div style={{ display:"flex", gap:12, fontSize:10, color:"#888", fontFamily:"monospace" }}>
              {(t.exit_time||t.entry_time) && <span>{(t.exit_time||t.entry_time).slice(0,16).replace("T"," ")}</span>}
              {t.size && <span>size <span style={{ color:ACC }}>${t.size?.toFixed(2)}</span></span>}
            </div>
          </div>
        );
        return (
          <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
            {todayTrades.length === 0
              ? <div style={{ color:"#bbb", textAlign:"center", padding:24 }}>Sin trades hoy</div>
              : todayTrades.slice(0,15).map((t,i)=><ScTradeRow key={i} t={t} i={i}/>)
            }
            {todayTrades.length > 0 && (
              <button style={verMasBtn} onClick={()=>setModal("trades")}>
                Ver más... ({todayTrades.length} trades hoy)
              </button>
            )}
            {modal==="trades" && (
              <VerMasModal title={`SCALPING — TRADES HOY (${todayTrades.length})`} onClose={()=>setModal(null)}>
                <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
                  {todayTrades.map((t,i)=><ScTradeRow key={i} t={t} i={i}/>)}
                </div>
              </VerMasModal>
            )}
          </div>
        );
      })()}

      {tab==="stats" && (
        <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:12 }}>
          {[
            { label:"Capital inicial", value:`$${(data.initial_capital||0).toFixed(2)}` },
            { label:"Capital actual",  value:`$${(data.current_capital||0).toFixed(2)}` },
            { label:"P&L total",       value:`${(data.total_pnl||0)>=0?"+":""}$${Math.abs(data.total_pnl||0).toFixed(2)}`, color:(data.total_pnl||0)>=0?"#00ff88":"#ff4444" },
            { label:"Win rate",        value:`${(data.win_rate||0).toFixed(1)}%`, color:"#ffcc00" },
            { label:"Max drawdown",    value:`${(data.max_drawdown||0).toFixed(1)}%`, color:"#ff8c00" },
            { label:"Trades totales",  value:data.total_trades||0 },
          ].map((s, i) => (
            <div key={i} style={{ background:"rgba(255,255,255,0.02)", borderRadius:8, padding:"10px 14px" }}>
              <div style={{ color:"#bbb", fontSize:10, marginBottom:4 }}>{s.label}</div>
              <div style={{ color:s.color||"#ccc", fontFamily:"monospace", fontWeight:700 }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      {tab==="log" && (()=>{
        const log = data.cycle_log||[];
        const ScLogRow = ({e,i}) => (
          <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
            <span style={{ color:"#bbb", minWidth:50 }}>{e.time}</span>
            <span style={{ color:e.msg?.includes("✓")?"#00ff88":e.msg?.includes("❌")?"#ff4444":e.msg?.includes("SCALP")||e.msg?.includes("SC-")?ACC:"#bbb" }}>{e.msg}</span>
          </div>
        );
        return (
          <div>
            <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
              {log.slice(0,20).map((e,i)=><ScLogRow key={i} e={e} i={i}/>)}
            </div>
            {log.length>0 && (
              <button style={verMasBtn} onClick={()=>setModal("log")}>
                Ver más... ({log.length} entradas)
              </button>
            )}
            {modal==="log" && (
              <VerMasModal title={`SCALPING — LOG HOY (${log.length})`} onClose={()=>setModal(null)}>
                <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
                  {log.map((e,i)=><ScLogRow key={i} e={e} i={i}/>)}
                </div>
              </VerMasModal>
            )}
          </div>
        );
      })()}
    </div>
  );
}

// ─── localStorage helpers ──────────────────────────────────────────────────────
function lsLoad(key, fallback) {
  try { const v = localStorage.getItem(key); if (v) return JSON.parse(v); } catch { /* ignore invalid local cache */ }
  return fallback;
}
function lsSave(key, data) {
  try { localStorage.setItem(key, JSON.stringify(data)); } catch { /* ignore storage quota/errors */ }
}

function getOpenPositions(data) {
  const list = [
    ...Object.values(data?.positions || {}),
    ...(data?.open_positions || []),
    ...(data?.open_trades || []),
  ];
  return list.filter((p, i, arr) => {
    const key = p.id || p.symbol || `${p.entry_time || ""}-${p.entry_price || ""}`;
    return arr.findIndex(x => (x.id || x.symbol || `${x.entry_time || ""}-${x.entry_price || ""}`) === key) === i;
  });
}

function getPositionSize(pos) {
  return Number(pos?.size_usdt ?? pos?.size ?? 0) || 0;
}

function getPositionSide(pos) {
  return pos?.side || pos?.direction || "LONG";
}

function getUnrealizedPnl(pos, liveprices = {}) {
  const entry = Number(pos?.entry_price || 0);
  const size = getPositionSize(pos);
  const price = Number(liveprices?.[pos?.symbol || "BTCUSDT"] || pos?.current_price || 0);
  if (!entry || !size || !price) return Number(pos?.pnl || 0) || 0;
  const leverage = Number(pos?.leverage || 1);
  const raw = getPositionSide(pos) === "LONG" ? (price - entry) / entry : (entry - price) / entry;
  return raw * leverage * size;
}

function getEquity(data, liveprices = {}) {
  const cash = Number(data?.current_capital || 0);
  const positions = getOpenPositions(data);
  const reserved = positions.reduce((sum, pos) => sum + getPositionSize(pos), 0);
  const unrealized = positions.reduce((sum, pos) => sum + getUnrealizedPnl(pos, liveprices), 0);
  return cash + reserved + unrealized;
}

function getEquityPnl(data, liveprices = {}) {
  return getEquity(data, liveprices) - Number(data?.initial_capital || 0);
}

// ─── Dashboard Principal ───────────────────────────────────────────────────────
export default function Dashboard() {
  const [scData,  setScData]  = useState(() => lsLoad("tbot_btc", MOCK_SC));
  const [asData,  setAsData]  = useState(() => lsLoad("tbot_alt", { bot:"altscalp", current_capital:200, initial_capital:200, total_pnl:0, win_rate:0, max_drawdown:0, positions:{}, closed_trades:[] }));
  const [liveprices, setLivePrices] = useState({});
  const lastManualClose = useRef(0);
  const manuallyClosed = useRef(new Set());
  const [blink, setBlink] = useState(true);
  const [time, setTime] = useState(new Date());
  const [lastFetch, setLastFetch] = useState("--");

  const LOCAL_API = "/api";

  const fetchStates = useCallback(async () => {
    const recentManualClose = Date.now() - lastManualClose.current < 60000;
    const load = async (bot, fallbackUrl, setter, lsKey) => {
      try {
        const d = await fetch(`${LOCAL_API}/state/${bot}?t=${Date.now()}`).then(r=>r.json());
        if (d && !d.error) {
          if (recentManualClose && manuallyClosed.current.size > 0) {
            const ids = manuallyClosed.current;
            if (d.open_trades) d.open_trades = d.open_trades.filter(t => !ids.has(t.id) && !ids.has(t.symbol));
            if (d.positions) Object.keys(d.positions).forEach(k => { if (ids.has(k) || ids.has(d.positions[k]?.id)) delete d.positions[k]; });
            if (d.open_positions) d.open_positions = d.open_positions.filter(p => !ids.has(p.symbol) && !ids.has(p.id));
          }
          lsSave(lsKey, d);
          setter(d); return;
        }
      } catch { /* use static fallback */ }
      try {
        const d = await fetch(fallbackUrl+"?t="+Date.now()).then(r=>r.json());
        if (d && !d.error) { lsSave(lsKey, d); setter(d); }
      } catch { /* keep previous state */ }
    };
    await load("btc",  "/paper_trading/scalping_state.json",  setScData,  "tbot_btc");
    await load("alt",  "/paper_trading/altscalp_state.json",  setAsData,  "tbot_alt");
    setLastFetch(new Date().toLocaleTimeString("es-AR"));
  }, [lastManualClose, manuallyClosed]);

  const fetchLivePrices = useCallback(async () => {
    const altScalpSyms = Object.values(asData.positions||{}).map(p=>p.symbol).filter(Boolean);
    const symbols = ["BTCUSDT", ...altScalpSyms];
    try {
      const prices = {};
      await Promise.all(symbols.map(async sym => {
        try {
          const r = await fetch(`https://fapi.binance.com/fapi/v1/ticker/price?symbol=${sym}`);
          const d = await r.json();
          if (d.price) prices[sym] = parseFloat(d.price);
        } catch { /* skip missing ticker */ }
      }));
      if (Object.keys(prices).length > 0) setLivePrices(prev => ({...prev, ...prices}));
    } catch { /* keep previous prices */ }
  }, [asData.positions]);

  useEffect(() => {
    const t1 = setInterval(()=>setBlink(b=>!b), 800);
    const t2 = setInterval(()=>setTime(new Date()), 1000);
    const t3 = setInterval(fetchStates, 15000);
    const initial = setTimeout(fetchStates, 0);
    return ()=>{ clearTimeout(initial); clearInterval(t1); clearInterval(t2); clearInterval(t3); };
  }, [fetchStates]);

  useEffect(() => {
    const initial = setTimeout(fetchLivePrices, 0);
    const t = setInterval(fetchLivePrices, 30000);
    return () => { clearTimeout(initial); clearInterval(t); };
  }, [fetchLivePrices]);

  const closePosition = useCallback(async (bot, pos, lp=liveprices) => {
    const isBtcBot   = bot === "btc";
    const isAltScalp = bot === "alt";
    const symbol     = pos.symbol || (isBtcBot ? "BTCUSDT" : pos.id) || "?";
    if (!confirm(`¿Cerrar posición de ${symbol} al precio actual?`)) return;

    // ── AltScalp: actualizar UI inmediatamente + señalar al bot ─────────────
    if (isAltScalp) {
      try {
        // 1. Calcular PnL con precio live o entrada
        const sym = symbol;
        let exitPrice = lp?.[sym];
        if (!exitPrice) {
          try {
            const ticker = await fetch(`https://fapi.binance.com/fapi/v1/ticker/price?symbol=${sym}`).then(r=>r.json());
            exitPrice = parseFloat(ticker.price) || 0;
          } catch { exitPrice = pos.entry_price || 0; }
        }
        if (!exitPrice || exitPrice <= 0) exitPrice = pos.entry_price || 0;

        const leverage = pos.leverage || 10;
        const side     = pos.direction || pos.side || "LONG";
        const size     = pos.size_usdt || pos.size || 0;
        const pnlPct   = side === "LONG"
          ? (exitPrice - pos.entry_price) / pos.entry_price * leverage
          : (pos.entry_price - exitPrice) / pos.entry_price * leverage;
        const pnl = parseFloat((size * pnlPct).toFixed(2));

        const closedTrade = {
          ...pos, exit_price: exitPrice,
          exit_time: new Date().toISOString(),
          exit_reason: "MANUAL", pnl,
          pnl_pct: parseFloat((pnlPct*100).toFixed(2)), status: "CLOSED"
        };

        // 2. Actualizar UI inmediatamente
        setAsData(prev => {
          const newPositions = {...(prev.positions||{})};
          delete newPositions[sym];
          const prevTrades = prev.total_trades || (prev.closed_trades||[]).length;
          const prevWins   = Math.round((prev.win_rate||0) / 100 * prevTrades);
          const newTotalPnl = parseFloat(((prev.total_pnl||0) + pnl).toFixed(2));
          const newCapital  = parseFloat(((prev.current_capital||200) + size + pnl).toFixed(2));
          const newClosed   = [...(prev.closed_trades||[]), closedTrade].slice(-100);
          return {
            ...prev,
            positions: newPositions,
            current_capital: newCapital,
            total_pnl: newTotalPnl,
            total_pnl_pct: parseFloat((newTotalPnl / (prev.initial_capital||200) * 100).toFixed(2)),
            closed_trades: newClosed,
            win_rate: parseFloat(((prevWins+(pnl>0?1:0))/(prevTrades+1)*100).toFixed(1)),
            total_trades: prevTrades + 1,
            cycle_log: [{time:new Date().toLocaleTimeString("es-AR"), msg:`🛑 MANUAL ${sym} @ $${exitPrice.toFixed(4)} | P&L ${pnl>=0?"+":""}$${pnl.toFixed(2)}`}, ...(prev.cycle_log||[]).slice(0,49)],
          };
        });
        lastManualClose.current = Date.now();
        manuallyClosed.current.add(symbol);
        setTimeout(() => manuallyClosed.current.delete(symbol), 60000);

        // 3. Señalar al bot via manual_close en disco
        const srvState = await fetch(`${LOCAL_API}/state/alt?t=${Date.now()}`).then(r=>r.json());
        await fetch(`${LOCAL_API}/state/alt`, {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({
            ...srvState,
            manual_close: [...(srvState.manual_close||[]), symbol],
          }),
        });
      } catch(e) { console.error("altscalp close error:", e); }
      return;
    }

    // ── Scalping / Altcoin: lógica existente ────────────────────────────────
    try {
      const botKey = isBtcBot ? "btc" : "alt";
      const state  = await fetch(`${LOCAL_API}/state/${botKey}?t=${Date.now()}`).then(r=>r.json());

      const sym = isBtcBot ? "BTCUSDT" : symbol;
      let exitPrice = lp?.[sym] || lp?.[symbol];
      if (!exitPrice) {
        try {
          const ticker = await fetch(`https://fapi.binance.com/fapi/v1/ticker/price?symbol=${sym}`).then(r=>r.json());
          exitPrice = parseFloat(ticker.price) || 0;
        } catch { exitPrice = pos.entry_price || 0; }
      }
      if (!exitPrice || exitPrice <= 0) exitPrice = pos.entry_price || 0;

      const leverage = pos.leverage || 3;
      const side     = pos.side || pos.direction || "LONG";
      const size     = pos.size || pos.size_usdt || 0;
      const pnlPct   = side === "LONG"
        ? (exitPrice - pos.entry_price) / pos.entry_price * leverage
        : (pos.entry_price - exitPrice) / pos.entry_price * leverage;
      const pnl = parseFloat((size * pnlPct).toFixed(2));

      const closedTrade = { ...pos, exit_price: exitPrice, exit_time: new Date().toISOString(),
        exit_reason: "MANUAL", pnl, pnl_pct: parseFloat((pnlPct*100).toFixed(2)), status: "CLOSED" };

      const closedTradeId = pos.id || symbol;
      const displayData   = isBtcBot ? scData : asData;
      const newOpenPositions = (displayData.open_positions||displayData.open_trades||[]).filter(p => p.symbol !== symbol && p.id !== closedTradeId);
      const newOpenTrades    = (displayData.open_trades||[]).filter(t => t.id !== closedTradeId && t.symbol !== symbol);
      const newPositions     = {...(displayData.positions||{})};
      delete newPositions[symbol]; delete newPositions[closedTradeId];

      const allClosed    = [...(state.all_closed_trades || state.closed_trades || displayData.all_closed_trades || displayData.closed_trades || []), closedTrade].slice(-100);
      const prevTotalPnl = displayData.total_pnl || state.total_pnl || 0;
      const totalPnl     = parseFloat((prevTotalPnl + pnl).toFixed(2));
      const capital      = parseFloat(((state.current_capital ?? displayData.current_capital ?? state.initial_capital ?? 200) + size + pnl).toFixed(2));
      const prevTrades   = displayData.total_trades || state.total_trades || allClosed.length;
      const prevWins     = Math.round((displayData.win_rate||0) / 100 * prevTrades);

      const newState = {
        ...state,
        positions: newPositions, open_positions: newOpenPositions, open_trades: newOpenTrades,
        all_closed_trades: allClosed, closed_trades: allClosed.slice(-30),
        total_pnl: totalPnl, total_pnl_raw: totalPnl,
        total_pnl_pct: parseFloat((totalPnl/(state.initial_capital||500)*100).toFixed(2)),
        current_capital: capital, capital,
        win_rate: parseFloat(((prevWins+(pnl>0?1:0))/(prevTrades+1)*100).toFixed(1)),
        total_trades: prevTrades + 1,
        cycle_log: [{time:new Date().toLocaleTimeString("es-AR"), msg:`🛑 MANUAL ${symbol} @ $${exitPrice.toFixed(2)} | P&L ${pnl>=0?"+":""}$${pnl.toFixed(2)}`}, ...(state.cycle_log||[]).slice(0,49)],
        manual_close: [closedTradeId || symbol],
        cooldowns: pnl < 0 ? {...(state.cooldowns||{}), [symbol]: new Date(Date.now()+20*60*1000).toISOString()} : (state.cooldowns||{}),
        last_updated: new Date().toISOString(),
      };

      if (isBtcBot) setScData(newState); else setAsData(newState);
      lastManualClose.current = Date.now();
      manuallyClosed.current.add(symbol);
      if (closedTradeId) manuallyClosed.current.add(closedTradeId);
      setTimeout(() => { manuallyClosed.current.delete(symbol); manuallyClosed.current.delete(closedTradeId); }, 5*60*1000);

      try {
        await fetch(`${LOCAL_API}/state/${botKey}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(newState)});
      } catch { /* UI already updated optimistically */ }
    } catch(e) { console.error(e); alert("Error: " + e.message); }
  }, [setScData, setAsData, scData, asData, lastManualClose, manuallyClosed, liveprices]);


  const scCapital    = getEquity(scData, liveprices);
  const asCapital    = getEquity(asData, liveprices);
  const scPnl        = getEquityPnl(scData, liveprices);
  const asPnl        = getEquityPnl(asData, liveprices);
  const totalPnl     = scPnl + asPnl;
  const totalCapital = scCapital + asCapital;

  return (
    <div style={{ background:"#050508", minHeight:"100vh", color:"#ccc", fontFamily:"'Courier New', monospace", padding:"0 0 40px" }}>
      {/* Top bar */}
      <div style={{ borderBottom:"1px solid rgba(255,255,255,0.06)", padding:"12px 28px", display:"flex", alignItems:"center", justifyContent:"space-between", background:"rgba(255,255,255,0.01)" }}>
        <div style={{ display:"flex", alignItems:"center", gap:12 }}>
          <div style={{ width:9, height:9, borderRadius:"50%", background:"#00ff88", boxShadow:"0 0 8px #00ff88", opacity:blink?1:0.2, transition:"opacity 0.3s" }} />
          <span style={{ color:"#00ff88", fontWeight:700, letterSpacing:3, fontSize:13 }}>TRADING BOT HQ</span>
          <span style={{ color:"#ccc", fontSize:11 }}>paper trading</span>
        </div>
        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
          <div style={{ textAlign:"right", cursor:"pointer" }} onClick={fetchStates} title="Click para actualizar">
            <div style={{ color:"#bbb", fontSize:10, marginBottom:2 }}>ACTUALIZADO ↻</div>
            <div style={{ color:"#bbb", fontSize:11 }}>{lastFetch}</div>
          </div>
          <span style={{ color:"#bbb", fontSize:11, marginLeft:8 }}>{time.toLocaleTimeString("es-AR")}</span>
        </div>
      </div>

      {/* Alerta Global de Liquidez */}
      {(scData.next_liquidity_check || asData.next_liquidity_check) && (
        <div style={{ margin: "10px 28px -10px", padding: "8px 16px", background: "rgba(255, 184, 0, 0.1)", border: "1px solid rgba(255, 184, 0, 0.3)", borderRadius: 8, color: "#ffb800", fontSize: 12, display: "flex", alignItems: "center", gap: 10 }}>
          <span>⚠️</span>
          <span>MODO AHORRO: El mercado está fuera de horas peak o con bajo volumen. Los bots están en espera.</span>
        </div>
      )}

      {/* P&L Summary bar */}
      {(() => {
        const bots = [
          { label:"BTC SCALP",  color:"#ff9933", data:scData  },
          { label:"ALT SCALP",  color:"#00ccff", data:asData  },
        ];
        return (
          <div style={{ borderBottom:"1px solid rgba(255,255,255,0.06)", padding:"10px 28px", display:"flex", alignItems:"center", gap:0, background:"rgba(0,0,0,0.2)" }}>
            {bots.map(({ label, color, data }, i) => {
              const pnl = getEquityPnl(data, liveprices);
              const cap = getEquity(data, liveprices);
              const wr  = data.win_rate || 0;
              const pos = pnl >= 0;
              return (
                <div key={label} style={{ flex:1, display:"flex", flexDirection:"column", alignItems:"center", padding:"4px 0", borderRight: i < 3 ? "1px solid rgba(255,255,255,0.06)" : "none" }}>
                  <div style={{ color, fontSize:9, letterSpacing:2, fontWeight:700, marginBottom:4 }}>{label}</div>
                  <div style={{ display:"flex", gap:14, alignItems:"center" }}>
                    <div style={{ textAlign:"center" }}>
                      <div style={{ color:"#bbb", fontSize:9, letterSpacing:1 }}>CAPITAL</div>
                      <div style={{ color:"#ccc", fontFamily:"monospace", fontSize:13, fontWeight:700 }}>${cap.toFixed(0)}</div>
                    </div>
                    <div style={{ textAlign:"center" }}>
                      <div style={{ color:"#bbb", fontSize:9, letterSpacing:1 }}>P&L</div>
                      <div style={{ color:pos?"#00ff88":"#ff4444", fontFamily:"monospace", fontSize:13, fontWeight:700 }}>{pos?"+":""}${pnl.toFixed(0)}</div>
                    </div>
                    <div style={{ textAlign:"center" }}>
                      <div style={{ color:"#bbb", fontSize:9, letterSpacing:1 }}>WR</div>
                      <div style={{ color:"#ffcc00", fontFamily:"monospace", fontSize:13, fontWeight:700 }}>{wr.toFixed(0)}%</div>
                    </div>
                  </div>
                </div>
              );
            })}
            {/* Total */}
            <div style={{ flex:1, display:"flex", flexDirection:"column", alignItems:"center", padding:"4px 0", borderLeft:"1px solid rgba(255,255,255,0.06)" }}>
              <div style={{ color:"#00ff88", fontSize:9, letterSpacing:2, fontWeight:700, marginBottom:4 }}>TOTAL</div>
              <div style={{ display:"flex", gap:14, alignItems:"center" }}>
                <div style={{ textAlign:"center" }}>
                  <div style={{ color:"#bbb", fontSize:9, letterSpacing:1 }}>CAPITAL</div>
                  <div style={{ color:"#ccc", fontFamily:"monospace", fontSize:13, fontWeight:700 }}>${totalCapital.toFixed(0)}</div>
                </div>
                <div style={{ textAlign:"center" }}>
                  <div style={{ color:"#bbb", fontSize:9, letterSpacing:1 }}>P&L</div>
                  <div style={{ color:totalPnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontSize:15, fontWeight:700 }}>{totalPnl>=0?"+":""}${totalPnl.toFixed(0)}</div>
                </div>
                <div style={{ textAlign:"center" }}>
                  <div style={{ color:"#bbb", fontSize:9, letterSpacing:1 }}>ROI</div>
                  <div style={{ color:totalPnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontSize:13, fontWeight:700 }}>{totalPnl>=0?"+":""}{(totalPnl/(((scData.initial_capital||0)+(asData.initial_capital||0))||1)*100).toFixed(0)}%</div>
                </div>
              </div>
            </div>
          </div>
        );
      })()}

      {/* Top row: BTC Scalp | Alt Scalp */}
      <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:20, padding:"20px 28px 0" }}>
        <ScalpingPanel data={scData} liveprices={liveprices} onClose={(pos)=>closePosition("btc", pos)} />
        <AltScalpPanel data={asData} liveprices={liveprices} onClose={(pos)=>closePosition("alt", pos)} />
      </div>

      <div style={{ margin:"0 28px", padding:"12px 18px", background:"rgba(0,255,136,0.03)", border:"1px solid rgba(0,255,136,0.1)", borderRadius:10, fontSize:11, color:"#ccc" }}>
        <span style={{ color:"#00ff88" }}>● PAPER TRADING</span> — Datos reales, dinero simulado. Actualizando cada 15s desde <code style={{ color:"#ccc" }}>localhost:8082</code>
      </div>
    </div>
  );
}

import React, { useState, useEffect, useCallback, useRef } from "react";

// ─── Mock fallback cuando no hay datos reales ──────────────────────────────
const MOCK_SP500 = {
  bot:"sp500", initial_capital:10000, current_capital:10000,
  total_pnl:0, total_pnl_pct:0, win_rate:0, total_trades:0,
  positions:{}, open_positions:[],
  closed_trades:[], cycle_log:[{time:"--:--", msg:"Esperando apertura del mercado US (9:30AM ET)..."}],
};

const MOCK_ALT = {
  bot:"altcoins", initial_capital:500, current_capital:500,
  total_pnl:0, total_pnl_pct:0, win_rate:0, total_trades:0,
  open_positions:[], positions:{}, closed_trades:[],
  cycle_log:[{time:"--:--", msg:"Esperando primer ciclo..."}],
  last_scan:[], scanning:false,
};

const MOCK_BN = {
  bot:"binance", initial_capital:500, current_capital:500, total_pnl:0,
  total_pnl_pct:0, win_rate:0, max_drawdown:0, trades_today:0,
  btc_price:0, rsi:50, trend:"neutral", macd_cross:"neutral", funding_rate:0, vol_ratio:1,
  open_trades:[], closed_trades:[], cycle_log:[{time:"--:--", msg:"Esperando primer ciclo..."}],
};

const MOCK_T2 = {
  bot:"trading2", initial_capital:500, current_capital:500,
  total_pnl:0, total_pnl_pct:0, win_rate:0, total_trades:0,
  active_strategy:"VOTE", btc_price:0,
  open_trades:[], closed_trades:[],
  last_vote:{ macd:"--", rsi_vwap:"--", cvd:"--", result:"--" },
  cycle_log:[{time:"--:--", msg:"Esperando primer ciclo..."}],
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
function BTCChart({ entryPrice, side }) {
  const [candles, setCandles] = useState([]);
  const [interval, setIntervalVal] = useState("1m");
  const W = 560, H = 180, PAD = 8;

  useEffect(() => {
    const load = async () => {
      try {
        const r = await fetch(`https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=${interval}&limit=60`);
        const data = await r.json();
        setCandles(data.map(k => ({
          o: parseFloat(k[1]), h: parseFloat(k[2]),
          l: parseFloat(k[3]), c: parseFloat(k[4]),
          t: parseInt(k[0]),
        })));
      } catch {}
    };
    load();
    const t = setInterval(load, interval === "1m" ? 10000 : 30000);
    return () => clearInterval(t);
  }, [interval]);

  if (!candles.length) return <div style={{ height:H, display:"flex", alignItems:"center", justifyContent:"center", color:"#ccc", fontSize:11 }}>Cargando gráfico...</div>;

  const prices = candles.flatMap(c => [c.h, c.l]);
  const minP = Math.min(...prices);
  const maxP = Math.max(...prices);
  const range = maxP - minP || 1;
  const toY = p => PAD + (1 - (p - minP) / range) * (H - PAD * 2);
  const YLAB = 40; // width reserved for Y-axis labels
  const cW = (W - PAD - YLAB) / candles.length;

  return (
    <div style={{ marginBottom:14 }}>
      <div style={{ display:"flex", gap:4, marginBottom:6 }}>
        {["1m","5m","15m","1h"].map(iv => (
          <button key={iv} onClick={()=>setIntervalVal(iv)} style={{ background:interval===iv?"rgba(255,184,0,0.15)":"transparent", border:`1px solid ${interval===iv?"#ffb80055":"#333"}`, color:interval===iv?"#ffb800":"#bbb", borderRadius:5, padding:"3px 8px", fontSize:10, cursor:"pointer", fontFamily:"monospace" }}>{iv}</button>
        ))}
        <span style={{ color:"#bbb", fontSize:10, marginLeft:8, alignSelf:"center" }}>
          ${candles[candles.length-1]?.c.toLocaleString()}
        </span>
      </div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ background:"rgba(0,0,0,0.2)", borderRadius:8 }}>
        {/* Grid lines + Y-axis price labels */}
        {[0, 0.25, 0.5, 0.75, 1].map(f => {
          const price = maxP - f * range;
          const y = PAD + f * (H - PAD * 2);
          return (
            <g key={f}>
              <line x1={PAD+36} y1={y} x2={W-PAD} y2={y} stroke="#ffffff08" strokeWidth="1" />
              <text x={PAD+34} y={y+3} fill="#ccc" fontSize="8" textAnchor="end">{price >= 1000 ? `${(price/1000).toFixed(1)}k` : price.toFixed(0)}</text>
            </g>
          );
        })}
        {/* Entry price line */}
        {entryPrice && (
          <>
            <line x1={PAD+36} y1={toY(entryPrice)} x2={W-PAD} y2={toY(entryPrice)}
              stroke={side==="LONG"?"#00ff8866":"#ff444466"} strokeWidth="1" strokeDasharray="4,4" />
            <text x={W-PAD-2} y={toY(entryPrice)-3} fill={side==="LONG"?"#00ff88":"#ff4444"} fontSize="8" textAnchor="end">entry</text>
          </>
        )}
        {/* Candles */}
        {candles.map((c, i) => {
          const x = YLAB + i * cW + cW * 0.1;
          const w = cW * 0.8;
          const cx = YLAB + i * cW + cW / 2;
          const bullish = c.c >= c.o;
          const color = bullish ? "#00ff88" : "#ff4444";
          const bodyTop = toY(Math.max(c.o, c.c));
          const bodyH = Math.max(1, Math.abs(toY(c.o) - toY(c.c)));
          return (
            <g key={i}>
              <line x1={cx} y1={toY(c.h)} x2={cx} y2={toY(c.l)} stroke={color} strokeWidth="0.8" opacity="0.7" />
              <rect x={x} y={bodyTop} width={w} height={bodyH} fill={color} opacity={bullish?0.8:0.9} />
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// ─── Panel Binance ─────────────────────────────────────────────────────────────
function BinancePanel({ data, liveprices, onClose }) {
  const [tab, setTab] = useState("position");
  const [btcScan, setBtcScan] = useState(null);
  const T = (id, label) => (
    <button onClick={()=>setTab(id)} style={{ background:tab===id?"rgba(255,184,0,0.1)":"transparent", border:`1px solid ${tab===id?"#ffb80055":"transparent"}`, color:tab===id?"#ffb800":"#bbb", borderRadius:7, padding:"5px 14px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>{label}</button>
  );

  // Soportar ambos formatos (open_trades array y positions dict)
  const openTradeFromPositions = data.positions ? Object.values(data.positions)[0] : null;
  const openTrade = openTradeFromPositions || (data.open_trades||[]).find(t=>t.bot==="binance");
  const closed = data.all_closed_trades || (data.closed_trades||[]).filter(t=>t.bot==="binance" || !t.bot);
  const posColor = openTrade?.side==="LONG"?"#00ff88":openTrade?.side==="SHORT"?"#ff4444":"#bbb";
  const btcLive = liveprices?.["BTCUSDT"] || data.btc_price || 0;
  const unrealizedRaw = openTrade && btcLive
    ? (openTrade.side==="LONG"
        ? (btcLive - openTrade.entry_price) / openTrade.entry_price
        : (openTrade.entry_price - btcLive) / openTrade.entry_price
      ) * (openTrade.leverage||3)
    : null;
  const unrealizedPct = unrealizedRaw !== null ? (unrealizedRaw * 100).toFixed(2) : null;
  const unrealizedUsd = unrealizedRaw !== null ? (unrealizedRaw * (openTrade.size||0)) : null;

  // Análisis técnico en vivo desde Binance public API
  useEffect(() => {
    const load = async () => {
      try {
        const r = await fetch("https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=100");
        const klines = await r.json();
        if (!klines.length) return;
        const closes = klines.map(k=>parseFloat(k[4]));
        const vols = klines.map(k=>parseFloat(k[5]));
        const diffs = closes.slice(1).map((c,i)=>c-closes[i]);
        const gains = diffs.map(d=>d>0?d:0);
        const losses = diffs.map(d=>d<0?-d:0);
        const avgGain = gains.slice(-14).reduce((a,b)=>a+b,0)/14;
        const avgLoss = losses.slice(-14).reduce((a,b)=>a+b,0)/14;
        const rsi = avgLoss===0?100:100-(100/(1+avgGain/avgLoss));
        const rsiV = rsi<30?"OVERSOLD":rsi>70?"OVERBOUGHT":"NEUTRAL";
        const sma20 = closes.slice(-20).reduce((a,b)=>a+b,0)/20;
        const std20 = Math.sqrt(closes.slice(-20).map(c=>(c-sma20)**2).reduce((a,b)=>a+b,0)/20);
        const bbPct = (closes[closes.length-1]-(sma20-2*std20))/(4*std20);
        const bbV = bbPct<0.2?"LOWER":bbPct>0.8?"UPPER":"MIDDLE";
        const ema = (arr,span) => arr.reduce((acc,v,i)=>i===0?[v]:[...acc,v*(2/(span+1))+acc[i-1]*(1-2/(span+1))],[]);
        const ema12 = ema(closes,12); const ema26 = ema(closes,26);
        const macd = ema12.map((v,i)=>v-ema26[i]);
        const signal = ema(macd,9);
        const hist = macd[macd.length-1]-signal[signal.length-1];
        const prevHist = macd[macd.length-2]-signal[signal.length-2];
        const macdCross = hist>0&&prevHist<=0?"bullish":hist<0&&prevHist>=0?"bearish":"neutral";
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
      } catch {}
    };
    load();
    const t = setInterval(load, 60000);
    return ()=>clearInterval(t);
  }, []);

  return (
    <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:16, padding:22, minWidth:0, overflow:"hidden" }}>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
        <div>
          <div style={{ color:"#ffb800", fontWeight:700, letterSpacing:2, fontSize:13 }}>BINANCE FUTURES</div>
          <div style={{ color:"#bbb", fontSize:11 }}>BTC/USDT · {data.leverage||3}x · paper trading</div>
        </div>
        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
          {btcLive > 0 && <span style={{ color:"#ffb800", fontFamily:"monospace", fontWeight:700 }}>${btcLive.toLocaleString()}</span>}
          <Badge text="PAPER" color="#ffb800" />
        </div>
      </div>

      <div style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)", flexWrap:"wrap", gap:12 }}>
        <Stat label="Capital" value={`$${(data.current_capital||0).toFixed(2)}`} />
        <PnlDisplay pnl={data.total_pnl||0} pct={data.total_pnl_pct||0} />
        <Stat label="RSI" value={data.rsi?.toFixed(1)||"--"} color={data.rsi<30?"#00ff88":data.rsi>70?"#ff4444":"#ccc"} />
        <Stat label="Win rate" value={`${(data.win_rate||0).toFixed(0)}%`} color="#ffcc00" size={18} />
      </div>

      {openTrade && (
        <div style={{ background:`${posColor}11`, border:`1px solid ${posColor}33`, borderRadius:10, padding:"12px 16px", marginBottom:14 }}>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:6 }}>
            <div style={{ display:"flex", gap:8, alignItems:"center" }}>
              <Badge text={openTrade.side} color={posColor} />
              <span style={{ color:"#bbb", fontSize:12 }}>entrada ${openTrade.entry_price?.toLocaleString()}</span>
              <Badge text={openTrade.confidence} color={openTrade.confidence==="HIGH"?"#00ff88":"#ffcc00"} />
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
                    <div style={{ color:col, fontWeight:700, fontSize:16, fontFamily:"monospace" }}>
                      {sign}{unrealizedUsd.toFixed(2)}$
                    </div>
                    <div style={{ color:col+"aa", fontWeight:700, fontSize:12, fontFamily:"monospace" }}>
                      {sign}{unrealizedPct}%
                    </div>
                  </div>
                );
              })()}
            </div>
          </div>
          <div style={{ display:"flex", gap:14, fontFamily:"monospace", fontSize:11 }}>
            <span style={{ color:"#bbb" }}>size <span style={{ color:"#ffb800" }}>${openTrade.size?.toFixed(2)}</span></span>
            <span style={{ color:"#bbb" }}>SL <span style={{ color:"#ff4444" }}>${openTrade.stop_loss?.toLocaleString()}</span></span>
            <span style={{ color:"#bbb" }}>TP <span style={{ color:"#00ff88" }}>${openTrade.take_profit?.toLocaleString()}</span></span>
          </div>
          {openTrade.reasoning && <div style={{ color:"#bbb", fontSize:11, marginTop:6 }}>{openTrade.reasoning?.slice(0,80)}...</div>}
        </div>
      )}

      <div style={{ display:"flex", gap:6, marginBottom:14, flexWrap:"wrap" }}>
        <Badge text={`Trend: ${data.trend||"--"}`} color={data.trend==="bullish"?"#00ff88":data.trend==="bearish"?"#ff4444":"#bbb"} />
        <Badge text={`MACD: ${data.macd_cross||"--"}`} color={data.macd_cross==="bullish"?"#00ff88":data.macd_cross==="bearish"?"#ff4444":"#bbb"} />
        <Badge text={`Vol: ${data.vol_ratio?.toFixed(1)||"--"}x`} color={data.vol_ratio>1.5?"#ffcc00":"#bbb"} />
        <Badge text={`Funding: ${data.funding_rate>=0?"+":""}${(data.funding_rate||0).toFixed(4)}%`} color={Math.abs(data.funding_rate||0)>0.01?"#ff8c00":"#bbb"} />
      </div>

      {btcScan && (
        <div style={{ background:"rgba(255,184,0,0.04)", border:"1px solid rgba(255,184,0,0.12)", borderRadius:8, padding:"10px 14px", marginBottom:14 }}>
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
          <BTCChart entryPrice={openTrade?.entry_price} side={openTrade?.side} />
          {!openTrade && (
            <div style={{ color:"#ccc", textAlign:"center", padding:12, fontSize:12 }}>Sin posición — Claude esperando señal</div>
          )}
          {openTrade && (
            <div style={{ color:"#bbb", fontSize:12, textAlign:"center" }}>
              Trade activo desde {openTrade.entry_time?.slice(0,16)?.replace("T"," ")}
            </div>
          )}
        </div>
      )}

      {tab==="trades" && (
        <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
          {closed.length===0
            ? <div style={{ color:"#bbb", textAlign:"center", padding:24 }}>Sin trades cerrados aún</div>
            : [...closed].reverse().slice(0,20).map((t,i)=>(
              <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"10px 14px", display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                <div style={{ display:"flex", gap:8, alignItems:"center", flexWrap:"wrap" }}>
                  <Badge text={t.side} color={t.side==="LONG"?"#00ff88":"#ff4444"} />
                  <Badge text={t.exit_reason||"--"} color={t.exit_reason==="TAKE_PROFIT"?"#00ff88":t.exit_reason==="STOP_LOSS"?"#ff4444":"#bbb"} />
                  <span style={{ color:"#bbb", fontSize:11 }}>${t.entry_price?.toLocaleString()} → ${t.exit_price?.toLocaleString()}</span>
                </div>
                <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}</span>
              </div>
            ))
          }
        </div>
      )}

      {tab==="stats" && (
        <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:12 }}>
          {[
            { label:"Capital inicial", value:`$${(data.initial_capital||0).toFixed(2)}` },
            { label:"Capital actual", value:`$${(data.current_capital||0).toFixed(2)}` },
            { label:"P&L total", value:`${(data.total_pnl||0)>=0?"+":""}$${Math.abs(data.total_pnl||0).toFixed(2)}`, color:(data.total_pnl||0)>=0?"#00ff88":"#ff4444" },
            { label:"Win rate", value:`${(data.win_rate||0).toFixed(1)}%`, color:"#ffcc00" },
            { label:"Max drawdown", value:`${(data.max_drawdown||0).toFixed(1)}%`, color:"#ff8c00" },
            { label:"Trades totales", value:closed.length + (openTrade?1:0) },
          ].map((s,i)=>(
            <div key={i} style={{ background:"rgba(255,255,255,0.02)", borderRadius:8, padding:"10px 14px" }}>
              <div style={{ color:"#bbb", fontSize:10, marginBottom:4 }}>{s.label}</div>
              <div style={{ color:s.color||"#ccc", fontFamily:"monospace", fontWeight:700 }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      {tab==="log" && (
        <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
          {(data.cycle_log||[]).slice(0,20).map((e,i)=>(
            <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
              <span style={{ color:"#bbb", minWidth:50 }}>{e.time}</span>
              <span style={{ color:e.msg?.includes("✓")?"#00ff88":e.msg?.includes("❌")?"#ff4444":e.msg?.includes("PAPER")?"#ffb800":"#bbb" }}>{e.msg}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Panel Altcoins ────────────────────────────────────────────────────────────
function AltcoinPanel({ data, liveprices, onClose }) {
  const [tab, setTab] = useState("positions");
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
          <div style={{ color:"#bbb", fontSize:11 }}>Top 20 por volumen · Claude AI · paper trading</div>
        </div>
        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
          {scanning && <div style={{ width:8, height:8, borderRadius:"50%", background:"#ff64c8" }} />}
          <Badge text="PAPER" color="#ff64c8" />
        </div>
      </div>

      <div style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)", flexWrap:"wrap", gap:12 }}>
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
            ? <div style={{ color:"#ccc", textAlign:"center", padding:24 }}>Sin posiciones abiertas — Claude escaneando mercado</div>
            : open.map((p,i)=>(
              <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.06)", borderRadius:10, padding:"12px 16px" }}>
                <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:6 }}>
                  <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                    <span style={{ color:"#ff64c8", fontWeight:700, fontFamily:"monospace" }}>{p.symbol}</span>
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
        </div>
      )}

      {tab==="scanner" && (
        <div>
          <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:12 }}>
            {scanning && <div style={{ width:8, height:8, borderRadius:"50%", background:"#ff64c8" }} />}
            <span style={{ color:"#bbb", fontSize:11 }}>{scanning ? "Escaneando mercado con Claude..." : `Último scan: ${scan.length} altcoins analizadas`}</span>
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

      {tab==="trades" && (
        <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
          {closed.length===0
            ? <div style={{ color:"#bbb", textAlign:"center", padding:24 }}>Sin trades cerrados aún</div>
            : [...closed].reverse().slice(0,15).map((t,i)=>(
              <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"10px 14px", display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                  <span style={{ color:"#ff64c8", fontFamily:"monospace", minWidth:80 }}>{t.symbol}</span>
                  <Badge text={t.direction||t.side} color={(t.direction||t.side)==="LONG"?"#00ff88":"#ff4444"} />
                  <Badge text={t.strategy} color={stratColor(t.strategy)} />
                  <Badge text={t.exit_reason||"CLOSE"} color={t.exit_reason==="TAKE_PROFIT"?"#00ff88":t.exit_reason==="STOP_LOSS"?"#ff4444":"#bbb"} />
                </div>
                <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}</span>
              </div>
            ))
          }
        </div>
      )}

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

      {tab==="log" && (
        <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
          {(data.cycle_log||[]).slice(0,15).map((e,i)=>(
            <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
              <span style={{ color:"#bbb", minWidth:50 }}>{e.time}</span>
              <span style={{ color:e.msg?.includes("✓")?"#00ff88":e.msg?.includes("❌")?"#ff4444":"#bbb" }}>{e.msg}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Panel SP500 ───────────────────────────────────────────────────────────────
function SP500Panel({ data }) {
  const [tab, setTab] = useState("positions");
  const T = (id, label) => (
    <button onClick={()=>setTab(id)} style={{ background:tab===id?"rgba(100,180,255,0.1)":"transparent", border:`1px solid ${tab===id?"#64b4ff55":"transparent"}`, color:tab===id?"#64b4ff":"#bbb", borderRadius:7, padding:"5px 14px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>{label}</button>
  );
  const open = data.open_positions || [];
  const closed = data.all_closed_trades || data.closed_trades || [];
  const wins = closed.filter(t=>t.pnl>0);

  return (
    <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:16, padding:22 }}>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
        <div>
          <div style={{ color:"#64b4ff", fontWeight:700, letterSpacing:2, fontSize:13 }}>RSI MEAN REVERSION — S&P500</div>
          <div style={{ color:"#bbb", fontSize:11 }}>Alpaca paper trading · Mercado US</div>
        </div>
        <div style={{ display:"flex", gap:8 }}>
          <Badge text="PAPER" color="#64b4ff" />
          <Badge text="ALPACA" color="#bbb" />
        </div>
      </div>

      <div style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)", flexWrap:"wrap", gap:12 }}>
        <Stat label="Capital" value={`$${(data.current_capital||0).toLocaleString()}`} />
        <PnlDisplay pnl={data.total_pnl||0} pct={data.total_pnl_pct||0} />
        <Stat label="Win Rate" value={`${(data.win_rate||0).toFixed(0)}%`} color="#ffcc00" />
        <Stat label="Trades" value={data.total_trades||0} color="#64b4ff" size={20} />
      </div>

      <div style={{ display:"flex", gap:6, marginBottom:14 }}>{T("positions","POSICIONES")}{T("trades","TRADES")}{T("stats","STATS")}{T("log","LOG")}</div>

      {tab==="positions" && (
        <div>
          {open.length===0
            ? <div style={{ color:"#ccc", textAlign:"center", padding:20 }}>Sin posiciones — el bot opera al cierre del mercado US</div>
            : open.map((p,i)=>(
              <div key={i} style={{ background:"rgba(100,180,255,0.06)", border:"1px solid rgba(100,180,255,0.2)", borderRadius:10, padding:"12px 16px", marginBottom:8 }}>
                <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                  <span style={{ color:"#64b4ff", fontWeight:700, fontFamily:"monospace" }}>{p.symbol}</span>
                  <Badge text="LONG" color="#00ff88" />
                  <span style={{ color:"#bbb", fontSize:12 }}>entrada ${p.entry_price?.toFixed(2)}</span>
                </div>
              </div>
            ))
          }
        </div>
      )}

      {tab==="trades" && (
        <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
          {closed.length===0
            ? <div style={{ color:"#ccc", textAlign:"center", padding:24 }}>Sin trades cerrados aún</div>
            : [...closed].reverse().slice(0,12).map((t,i)=>(
              <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"10px 14px", display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                  <span style={{ color:"#64b4ff", fontFamily:"monospace", minWidth:60 }}>{t.symbol}</span>
                  <Badge text={t.exit_reason||"CLOSE"} color={t.exit_reason==="TAKE_PROFIT"?"#00ff88":t.exit_reason==="STOP_LOSS"?"#ff4444":"#bbb"} />
                  <span style={{ color:"#bbb", fontSize:11 }}>{t.date||t.exit_time?.slice(0,10)}</span>
                </div>
                <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}</span>
              </div>
            ))
          }
        </div>
      )}

      {tab==="stats" && (
        <div style={{ display:"grid", gridTemplateColumns:"repeat(4, 1fr)", gap:10 }}>
          {[
            { label:"Capital inicial", value:`$${(data.initial_capital||0).toLocaleString()}` },
            { label:"Capital actual", value:`$${(data.current_capital||0).toLocaleString()}` },
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

      {tab==="log" && (
        <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
          {(data.cycle_log||[]).slice(0,15).map((e,i)=>(
            <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
              <span style={{ color:"#bbb", minWidth:50 }}>{e.time}</span>
              <span style={{ color:e.msg?.includes("✓")?"#00ff88":e.msg?.includes("❌")?"#ff4444":"#bbb" }}>{e.msg}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Panel Trading2 ───────────────────────────────────────────────────────────
function Trading2Panel({ data, liveprices, onClose }) {
  const [tab, setTab] = useState("position");
  const T = (id, label) => (
    <button onClick={()=>setTab(id)} style={{ background:tab===id?"rgba(136,139,255,0.12)":"transparent", border:`1px solid ${tab===id?"#888bff55":"transparent"}`, color:tab===id?"#888bff":"#bbb", borderRadius:7, padding:"5px 14px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>{label}</button>
  );

  const openTrade = (data.open_trades||[]).find(t => t.bot==="trading2" || !t.bot) || null;
  const closed    = data.closed_trades || [];
  const wins      = closed.filter(t => t.pnl > 0);
  const btcLive   = liveprices?.["BTCUSDT"] || data.btc_price || 0;
  const vote      = data.last_vote || {};
  const activeStrat = data.active_strategy || "VOTE";

  const posColor  = openTrade?.side==="LONG" ? "#00ff88" : openTrade?.side==="SHORT" ? "#ff4444" : "#888bff";

  const unrealizedRaw = openTrade && btcLive
    ? (openTrade.side==="LONG"
        ? (btcLive - openTrade.entry_price) / openTrade.entry_price
        : (openTrade.entry_price - btcLive) / openTrade.entry_price
      ) * (openTrade.leverage || 3)
    : null;
  const unrealizedPct = unrealizedRaw !== null ? (unrealizedRaw * 100).toFixed(2) : null;
  const unrealizedUsd = unrealizedRaw !== null ? (unrealizedRaw * (openTrade.size || 0)) : null;

  // Colores por voto individual
  const voteColor = v => v==="LONG"?"#00ff88":v==="SHORT"?"#ff4444":v==="FLAT"?"#555":"#888bff";
  const stratLabel = s => ({ MACD:"MACD", RSI_VWAP:"RSI+VWAP", CVD:"CVD", VOTE:"VOTE" }[s] || s);

  return (
    <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(136,139,255,0.15)", borderRadius:16, padding:22, minWidth:0, overflow:"hidden" }}>
      {/* Header */}
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
        <div>
          <div style={{ color:"#888bff", fontWeight:700, letterSpacing:2, fontSize:13 }}>TRADING2 · 3 ESTRATEGIAS</div>
          <div style={{ color:"#bbb", fontSize:11 }}>BTC/USDT · MACD · RSI+VWAP · CVD · modo {activeStrat}</div>
        </div>
        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
          {btcLive > 0 && <span style={{ color:"#888bff", fontFamily:"monospace", fontWeight:700 }}>${btcLive.toLocaleString()}</span>}
          <Badge text="PAPER" color="#888bff" />
          <Badge text={stratLabel(activeStrat)} color="#888bff" />
        </div>
      </div>

      {/* Stats row */}
      <div style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)", flexWrap:"wrap", gap:12 }}>
        <Stat label="Capital"   value={`$${(data.current_capital||0).toFixed(2)}`} />
        <PnlDisplay pnl={data.total_pnl||0} pct={data.total_pnl_pct||0} />
        <Stat label="Win rate"  value={`${(data.win_rate||0).toFixed(0)}%`} color="#ffcc00" />
        <Stat label="Trades"    value={data.total_trades||0} color="#888bff" size={20} />
      </div>

      {/* Voting panel (solo en modo VOTE) */}
      {activeStrat === "VOTE" && (
        <div style={{ background:"rgba(136,139,255,0.05)", border:"1px solid rgba(136,139,255,0.15)", borderRadius:10, padding:"10px 16px", marginBottom:14 }}>
          <div style={{ color:"#888bff", fontSize:10, letterSpacing:1, marginBottom:8 }}>VOTACIÓN</div>
          <div style={{ display:"flex", gap:10, flexWrap:"wrap", alignItems:"center" }}>
            {[
              { label:"MACD",     key:"macd"    },
              { label:"RSI+VWAP", key:"rsi_vwap"},
              { label:"CVD",      key:"cvd"     },
            ].map(({ label, key }) => (
              <div key={key} style={{ textAlign:"center" }}>
                <div style={{ color:"#bbb", fontSize:9, letterSpacing:1, marginBottom:3 }}>{label}</div>
                <div style={{ background:voteColor(vote[key])+"22", border:`1px solid ${voteColor(vote[key])}44`, borderRadius:6, padding:"3px 10px", color:voteColor(vote[key]), fontWeight:700, fontSize:11, fontFamily:"monospace" }}>
                  {vote[key] || "--"}
                </div>
              </div>
            ))}
            <div style={{ marginLeft:8, borderLeft:"1px solid rgba(255,255,255,0.08)", paddingLeft:12 }}>
              <div style={{ color:"#bbb", fontSize:9, letterSpacing:1, marginBottom:3 }}>RESULTADO</div>
              <div style={{ color:voteColor(vote.result), fontWeight:700, fontSize:14, fontFamily:"monospace" }}>
                {vote.result || "--"}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Open trade */}
      {openTrade && (
        <div style={{ background:`${posColor}11`, border:`1px solid ${posColor}33`, borderRadius:10, padding:"12px 16px", marginBottom:14 }}>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:6 }}>
            <div style={{ display:"flex", gap:8, alignItems:"center" }}>
              <Badge text={openTrade.side} color={posColor} />
              <span style={{ color:"#bbb", fontSize:12 }}>entrada ${openTrade.entry_price?.toLocaleString()}</span>
              {openTrade._strategy && <Badge text={openTrade._strategy} color="#888bff" />}
              <Badge text={openTrade.confidence} color={openTrade.confidence==="HIGH"?"#00ff88":"#ffcc00"} />
            </div>
            <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:4 }}>
              {onClose && (
                <button onClick={()=>onClose(openTrade)} style={{ background:"rgba(255,68,68,0.15)", border:"1px solid #ff444455", color:"#ff6666", borderRadius:6, padding:"4px 10px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>
                  CERRAR
                </button>
              )}
              {unrealizedPct && (() => {
                const col  = parseFloat(unrealizedPct) >= 0 ? "#00ff88" : "#ff4444";
                const sign = parseFloat(unrealizedPct) >= 0 ? "+" : "";
                return (
                  <div style={{ textAlign:"right" }}>
                    <div style={{ color:col, fontWeight:700, fontSize:16, fontFamily:"monospace" }}>{sign}{unrealizedUsd.toFixed(2)}$</div>
                    <div style={{ color:col+"aa", fontSize:12, fontFamily:"monospace" }}>{sign}{unrealizedPct}%</div>
                  </div>
                );
              })()}
            </div>
          </div>
          <div style={{ display:"flex", gap:14, fontFamily:"monospace", fontSize:11 }}>
            <span style={{ color:"#bbb" }}>size <span style={{ color:"#888bff" }}>${openTrade.size?.toFixed(2)}</span></span>
            <span style={{ color:"#bbb" }}>SL <span style={{ color:"#ff4444" }}>${openTrade.stop_loss?.toLocaleString()}</span></span>
            <span style={{ color:"#bbb" }}>TP <span style={{ color:"#00ff88" }}>${openTrade.take_profit?.toLocaleString()}</span></span>
          </div>
          {openTrade.reasoning && (
            <div style={{ color:"#bbb", fontSize:11, marginTop:6 }}>{openTrade.reasoning?.slice(0, 90)}...</div>
          )}
        </div>
      )}
      {!openTrade && (
        <div style={{ color:"#bbb", textAlign:"center", padding:"10px 0 14px", fontSize:12 }}>
          Sin posición — esperando señal ({activeStrat})
        </div>
      )}

      {/* Chart */}
      <BTCChart entryPrice={openTrade?.entry_price} side={openTrade?.side} />

      {/* Tabs */}
      <div style={{ display:"flex", gap:6, marginBottom:14, flexWrap:"wrap" }}>
        {T("position","POSICIÓN")}{T("trades","TRADES")}{T("stats","STATS")}{T("log","LOG")}
      </div>

      {tab==="position" && !openTrade && (
        <div style={{ color:"#bbb", textAlign:"center", padding:20, fontSize:12 }}>
          Sin posición abierta
        </div>
      )}

      {tab==="trades" && (
        <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
          {closed.length === 0
            ? <div style={{ color:"#bbb", textAlign:"center", padding:24 }}>Sin trades cerrados aún</div>
            : [...closed].reverse().slice(0, 20).map((t, i) => (
              <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"10px 14px", display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                <div style={{ display:"flex", gap:8, alignItems:"center", flexWrap:"wrap" }}>
                  <Badge text={t.side} color={t.side==="LONG"?"#00ff88":"#ff4444"} />
                  {t._strategy && <Badge text={t._strategy} color="#888bff" />}
                  <Badge text={t.exit_reason||"--"} color={t.exit_reason==="TAKE_PROFIT"?"#00ff88":t.exit_reason==="STOP_LOSS"?"#ff4444":"#bbb"} />
                  <span style={{ color:"#bbb", fontSize:11 }}>${t.entry_price?.toLocaleString()} → ${t.exit_price?.toLocaleString()}</span>
                </div>
                <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}</span>
              </div>
            ))
          }
        </div>
      )}

      {tab==="stats" && (
        <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:12 }}>
          {[
            { label:"Capital inicial", value:`$${(data.initial_capital||0).toFixed(2)}` },
            { label:"Capital actual",  value:`$${(data.current_capital||0).toFixed(2)}` },
            { label:"P&L total",       value:`${(data.total_pnl||0)>=0?"+":""}$${Math.abs(data.total_pnl||0).toFixed(2)}`, color:(data.total_pnl||0)>=0?"#00ff88":"#ff4444" },
            { label:"Win rate",        value:`${(data.win_rate||0).toFixed(1)}%`, color:"#ffcc00" },
            { label:"Total trades",    value:closed.length + (openTrade?1:0) },
            { label:"Ganadores",       value:wins.length, color:"#00ff88" },
            { label:"Perdedores",      value:(data.total_trades||0)-wins.length, color:"#ff4444" },
            { label:"Mejor trade",     value:closed.length?`$${Math.max(...closed.map(t=>t.pnl||0)).toFixed(2)}`:"--", color:"#00ff88" },
          ].map((s, i) => (
            <div key={i} style={{ background:"rgba(255,255,255,0.02)", borderRadius:8, padding:"10px 14px" }}>
              <div style={{ color:"#bbb", fontSize:10, marginBottom:4 }}>{s.label}</div>
              <div style={{ color:s.color||"#ccc", fontFamily:"monospace", fontWeight:700 }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      {tab==="log" && (
        <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
          {(data.cycle_log||[]).slice(0, 20).map((e, i) => (
            <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
              <span style={{ color:"#bbb", minWidth:50 }}>{e.time}</span>
              <span style={{ color:e.msg?.includes("✓")?"#00ff88":e.msg?.includes("❌")?"#ff4444":e.msg?.includes("VOTE")?"#888bff":"#bbb" }}>{e.msg}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── localStorage helpers ──────────────────────────────────────────────────────
const LS_KEY = { altcoins: "tbot_alt", binance: "tbot_bn", sp500: "tbot_sp500", trading2: "tbot_t2" };
function lsLoad(key, fallback) {
  try { const v = localStorage.getItem(key); if (v) return JSON.parse(v); } catch {}
  return fallback;
}
function lsSave(key, data) {
  try { localStorage.setItem(key, JSON.stringify(data)); } catch {}
}

// ─── Dashboard Principal ───────────────────────────────────────────────────────
export default function Dashboard() {
  const [bnData, setBnData]   = useState(() => lsLoad(LS_KEY.binance,   MOCK_BN));
  const [altData, setAltData] = useState(() => lsLoad(LS_KEY.altcoins,  MOCK_ALT));
  const [sp500Data, setSp500Data] = useState(() => lsLoad(LS_KEY.sp500, MOCK_SP500));
  const [t2Data, setT2Data]   = useState(() => lsLoad(LS_KEY.trading2,  MOCK_T2));
  const [liveprices, setLivePrices] = useState({});
  const lastManualClose = useRef(0);
  const manuallyClosed = useRef(new Set());
  const [blink, setBlink] = useState(true);
  const [time, setTime] = useState(new Date());
  const [lastFetch, setLastFetch] = useState("--");

  const LOCAL_API = "http://localhost:8765";

  const fetchStates = useCallback(async () => {
    const recentManualClose = Date.now() - lastManualClose.current < 30000;
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
      } catch {}
      try {
        const d = await fetch(fallbackUrl+"?t="+Date.now()).then(r=>r.json());
        if (d && !d.error) { lsSave(lsKey, d); setter(d); }
      } catch {}
    };
    await load("altcoins", "/altcoin_data/state.json",          setAltData,  LS_KEY.altcoins);
    await load("binance",  "/paper_trading/binance_state.json", setBnData,   LS_KEY.binance);
    await load("sp500",    "/sp500_data/state.json",            setSp500Data,LS_KEY.sp500);
    await load("trading2", "/trading2_data/state.json",         setT2Data,   LS_KEY.trading2);
    setLastFetch(new Date().toLocaleTimeString("es-AR"));
  }, [lastManualClose, manuallyClosed]);

  const fetchLivePrices = useCallback(async () => {
    const symbols = ["BTCUSDT", ...(altData.open_positions||[]).map(p=>p.symbol).filter(Boolean)];
    try {
      const prices = {};
      await Promise.all(symbols.map(async sym => {
        try {
          const r = await fetch(`https://fapi.binance.com/fapi/v1/ticker/price?symbol=${sym}`);
          const d = await r.json();
          if (d.price) prices[sym] = parseFloat(d.price);
        } catch {}
      }));
      if (Object.keys(prices).length > 0) setLivePrices(prev => ({...prev, ...prices}));
    } catch {}
  }, [altData.open_positions]);

  useEffect(() => {
    const t1 = setInterval(()=>setBlink(b=>!b), 800);
    const t2 = setInterval(()=>setTime(new Date()), 1000);
    const t3 = setInterval(fetchStates, 15000);
    fetchStates();
    return ()=>{ clearInterval(t1); clearInterval(t2); clearInterval(t3); };
  }, [fetchStates]);

  useEffect(() => {
    fetchLivePrices();
    const t = setInterval(fetchLivePrices, 30000);
    return () => clearInterval(t);
  }, [fetchLivePrices]);

  const closePosition = useCallback(async (bot, pos, lp=liveprices) => {
    const symbol = pos.symbol || pos.id || (bot === "binance" ? "BTCUSDT" : "?");
    if (!confirm(`¿Cerrar posición de ${symbol} al precio actual?`)) return;
    try {
      const botKey = bot === "binance" ? "binance" : "altcoins";
      const state = await fetch(`${LOCAL_API}/state/${botKey}?t=${Date.now()}`).then(r=>r.json());
      if (!pos) { alert("No se encontró la posición"); return; }

      const sym = bot === "binance" ? "BTCUSDT" : symbol;
      let exitPrice = lp?.[sym] || lp?.[symbol];
      if (!exitPrice) {
        try {
          const ticker = await fetch(`https://fapi.binance.com/fapi/v1/ticker/price?symbol=${sym}`).then(r=>r.json());
          exitPrice = parseFloat(ticker.price) || 0;
        } catch { exitPrice = pos.entry_price || 0; }
      }

      const leverage = pos.leverage || 3;
      const side = pos.side || pos.direction || "LONG";
      const size = pos.size || pos.size_usdt || 0;
      const pnlPct = side === "LONG"
        ? (exitPrice - pos.entry_price) / pos.entry_price * leverage
        : (pos.entry_price - exitPrice) / pos.entry_price * leverage;
      const pnl = parseFloat((size * pnlPct).toFixed(2));

      const closedTrade = { ...pos, exit_price: exitPrice, exit_time: new Date().toISOString(),
        exit_reason: "MANUAL", pnl, pnl_pct: parseFloat((pnlPct*100).toFixed(2)), status: "CLOSED" };

      const closedTradeId = pos.id || symbol;
      // Usar el estado del DASHBOARD (lo que se ve en pantalla) para filtrar posiciones,
      // no el del servidor que puede estar desactualizado o vacío por un restart del bot
      const displayData = bot === "binance" ? bnData : altData;
      const newOpenPositions = (displayData.open_positions||displayData.open_trades||[]).filter(p => p.symbol !== symbol && p.id !== closedTradeId);
      const newOpenTrades = (displayData.open_trades||[]).filter(t => t.id !== closedTradeId && t.symbol !== symbol);
      const newPositions = {...(displayData.positions||{})};
      delete newPositions[symbol];
      delete newPositions[closedTradeId];

      // Para closed trades usar el servidor (tiene el historial completo) o el display como fallback
      const allClosed = [...(state.all_closed_trades || state.closed_trades || displayData.all_closed_trades || displayData.closed_trades || []), closedTrade];
      const totalPnl = parseFloat(allClosed.reduce((s,t) => s+(t.pnl||0), 0).toFixed(2));
      const wins = allClosed.filter(t => t.pnl > 0);
      const reservado = newOpenPositions.reduce((s,p) => s+(p.size||p.size_usdt||0), 0);
      const capital = parseFloat(((state.initial_capital||500) - reservado + totalPnl).toFixed(2));

      const newState = {
        ...state,
        positions: newPositions,
        open_positions: newOpenPositions,
        open_trades: newOpenTrades,
        all_closed_trades: allClosed,
        closed_trades: allClosed.slice(-30),
        total_pnl: totalPnl, total_pnl_raw: totalPnl,
        total_pnl_pct: parseFloat((totalPnl/(state.initial_capital||500)*100).toFixed(2)),
        current_capital: capital, capital,
        win_rate: allClosed.length ? parseFloat((wins.length/allClosed.length*100).toFixed(1)) : 0,
        total_trades: allClosed.length,
        cycle_log: [{ time: new Date().toLocaleTimeString("es-AR"),
          msg: `🛑 MANUAL ${symbol} @ $${exitPrice.toFixed(2)} | P&L ${pnl>=0?"+":""}$${pnl.toFixed(2)}` },
          ...(state.cycle_log||[]).slice(0,49)],
        manual_close: bot === "binance" ? true : [symbol],
        cooldowns: { ...(state.cooldowns||{}), [symbol]: new Date(Date.now()+2*60*60*1000).toISOString() },
        last_updated: new Date().toISOString(),
      };

      if (bot === "binance") setBnData(newState); else setAltData(newState);
      lastManualClose.current = Date.now();
      manuallyClosed.current.add(symbol);
      if (closedTradeId) manuallyClosed.current.add(closedTradeId);
      setTimeout(() => { manuallyClosed.current.delete(symbol); manuallyClosed.current.delete(closedTradeId); }, 5 * 60 * 1000);

      try {
        await fetch(`${LOCAL_API}/state/${botKey}`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(newState)
        });
      } catch {}

    } catch(e) { console.error(e); alert("Error: " + e.message); }
  }, [setBnData, setAltData, bnData, altData, lastManualClose, manuallyClosed, liveprices]);

  const totalPnl     = (bnData.total_pnl||0) + (altData.total_pnl||0) + (sp500Data.total_pnl||0) + (t2Data.total_pnl||0);
  const totalCapital = (bnData.current_capital||0) + (altData.current_capital||0) + (sp500Data.current_capital||0) + (t2Data.current_capital||0);

  return (
    <div style={{ background:"#050508", minHeight:"100vh", color:"#ccc", fontFamily:"'Courier New', monospace", padding:"0 0 40px" }}>
      <div style={{ borderBottom:"1px solid rgba(255,255,255,0.06)", padding:"14px 28px", display:"flex", alignItems:"center", justifyContent:"space-between", background:"rgba(255,255,255,0.01)" }}>
        <div style={{ display:"flex", alignItems:"center", gap:12 }}>
          <div style={{ width:9, height:9, borderRadius:"50%", background:"#00ff88", boxShadow:"0 0 8px #00ff88", opacity:blink?1:0.2, transition:"opacity 0.3s" }} />
          <span style={{ color:"#00ff88", fontWeight:700, letterSpacing:3, fontSize:13 }}>TRADING BOT HQ</span>
          <span style={{ color:"#ccc", fontSize:11 }}>paper trading</span>
        </div>
        <div style={{ display:"flex", gap:24, alignItems:"center" }}>
          <div style={{ textAlign:"center" }}>
            <div style={{ color:"#bbb", fontSize:10, marginBottom:2 }}>CAPITAL TOTAL</div>
            <div style={{ color:"#ccc", fontFamily:"monospace", fontSize:15, fontWeight:700 }}>${totalCapital.toFixed(2)}</div>
          </div>
          <div style={{ textAlign:"center" }}>
            <div style={{ color:"#bbb", fontSize:10, marginBottom:2 }}>P&L COMBINADO</div>
            <div style={{ color:totalPnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontSize:15, fontWeight:700 }}>{totalPnl>=0?"+":""}${totalPnl.toFixed(2)}</div>
          </div>
          <div style={{ textAlign:"right", cursor:"pointer" }} onClick={fetchStates} title="Click para actualizar">
            <div style={{ color:"#bbb", fontSize:10, marginBottom:2 }}>ACTUALIZADO ↻</div>
            <div style={{ color:"#bbb", fontSize:11 }}>{lastFetch}</div>
          </div>
          <span style={{ color:"#bbb", fontSize:11 }}>{time.toLocaleTimeString("es-AR")}</span>
        </div>
      </div>

      {/* Main grid: BTC left | Altcoins right */}
      <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:20, padding:"20px 28px" }}>
        <BinancePanel data={bnData} liveprices={liveprices} onClose={(pos)=>closePosition("binance", pos)} />
        <AltcoinPanel data={altData} liveprices={liveprices} onClose={(pos)=>closePosition("altcoin", pos)} />
      </div>

      {/* Trading2 full width below BTC/Alt */}
      <div style={{ padding:"0 28px 20px" }}>
        <Trading2Panel data={t2Data} liveprices={liveprices} onClose={(pos)=>closePosition("binance", pos)} />
      </div>

      {/* SP500 full width below */}
      <div style={{ padding:"0 28px 20px" }}>
        <SP500Panel data={sp500Data} />
      </div>

      <div style={{ margin:"0 28px", padding:"12px 18px", background:"rgba(0,255,136,0.03)", border:"1px solid rgba(0,255,136,0.1)", borderRadius:10, fontSize:11, color:"#ccc" }}>
        <span style={{ color:"#00ff88" }}>● PAPER TRADING</span> — Datos reales, dinero simulado. Actualizando cada 15s desde <code style={{ color:"#ccc" }}>localhost:8765</code>
      </div>
    </div>
  );
}

import { useState, useEffect, useCallback } from "react";

// ─── Mock fallback cuando no hay datos reales ──────────────────────────────
// ─── Mock fallback cuando no hay datos reales ──────────────────────────────
const MOCK_PM = {
  bot:"polymarket", initial_capital:300, current_capital:300, total_pnl:0,
  total_pnl_pct:0, win_rate:0, max_drawdown:0, trades_today:0,
  open_trades:[], closed_trades:[], cycle_log:[{time:"--:--", msg:"Esperando primer ciclo..."}],
};
const MOCK_RSI = {
  bot:"rsi_sp500", initial_capital:10000, current_capital:10000,
  total_pnl:0, total_pnl_pct:0, win_rate:0, total_trades:0, trades_today:0,
  basket:["VLO","AMAT","EOG","MOS","COST","EQIX","GILD"], rsi_period:10,
  current_position:null, entry_price:null,
  closed_trades:[], cycle_log:[{time:"--:--", msg:"Esperando apertura del mercado US..."}],
};

const MOCK_ALT = {
  bot:"altcoins", initial_capital:500, current_capital:500,
  total_pnl:0, total_pnl_pct:0, win_rate:0, total_trades:0,
  open_positions:[], closed_trades:[],
  cycle_log:[{time:"--:--", msg:"Esperando primer ciclo..."}],
};

const MOCK_BN = {
  bot:"binance", initial_capital:500, current_capital:500, total_pnl:0,
  total_pnl_pct:0, win_rate:0, max_drawdown:0, trades_today:0,
  btc_price:0, rsi:50, trend:"neutral", macd_cross:"neutral", funding_rate:0, vol_ratio:1,
  open_trades:[], closed_trades:[], cycle_log:[{time:"--:--", msg:"Esperando primer ciclo..."}],
};

const Badge = ({ text, color }) => (
  <span style={{ background:color+"22", border:`1px solid ${color}44`, color, borderRadius:6, padding:"2px 8px", fontSize:11, fontWeight:700, letterSpacing:1 }}>{text}</span>
);

const Stat = ({ label, value, color="#ccc", size=22 }) => (
  <div style={{ textAlign:"center" }}>
    <div style={{ color:"#666", fontSize:10, letterSpacing:1, textTransform:"uppercase", marginBottom:3 }}>{label}</div>
    <div style={{ color, fontSize:size, fontWeight:700, fontFamily:"monospace" }}>{value}</div>
  </div>
);

const PnlDisplay = ({ pnl, pct }) => {
  const pos = pnl >= 0;
  return (
    <div style={{ textAlign:"center" }}>
      <div style={{ color:"#666", fontSize:10, letterSpacing:1, textTransform:"uppercase", marginBottom:3 }}>P&L</div>
      <div style={{ color:pos?"#00ff88":"#ff4444", fontSize:20, fontWeight:700, fontFamily:"monospace" }}>{pos?"+":""}${Math.abs(pnl).toFixed(2)}</div>
      <div style={{ color:pos?"#00ff8866":"#ff444466", fontSize:11 }}>{pos?"+":""}{pct?.toFixed(1)}%</div>
    </div>
  );
};

// ─── Panel Binance ─────────────────────────────────────────────────────────────
function BinancePanel({ data }) {
  const [tab, setTab] = useState("position");
  const T = (id, label) => (
    <button onClick={()=>setTab(id)} style={{ background:tab===id?"rgba(255,184,0,0.1)":"transparent", border:`1px solid ${tab===id?"#ffb80055":"transparent"}`, color:tab===id?"#ffb800":"#555", borderRadius:7, padding:"5px 14px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>{label}</button>
  );

  const openTrade = (data.open_trades||[]).find(t=>t.bot==="binance");
  const closed = (data.closed_trades||[]).filter(t=>t.bot==="binance");
  const posColor = openTrade?.side==="LONG"?"#00ff88":openTrade?.side==="SHORT"?"#ff4444":"#666";
  const unrealizedPct = openTrade && data.btc_price
    ? ((openTrade.side==="LONG"
        ? (data.btc_price - openTrade.entry_price)/openTrade.entry_price
        : (openTrade.entry_price - data.btc_price)/openTrade.entry_price
      ) * (openTrade.leverage||3) * 100).toFixed(2)
    : null;

  return (
    <div style={{ flex:1, minWidth:300, background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:16, padding:22 }}>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
        <div>
          <div style={{ color:"#ffb800", fontWeight:700, letterSpacing:2, fontSize:13 }}>BINANCE FUTURES</div>
          <div style={{ color:"#555", fontSize:11 }}>BTC/USDT · {data.leverage||3}x · paper trading</div>
        </div>
        <Badge text="PAPER" color="#ffb800" />
      </div>

      <div style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)" }}>
        <Stat label="Capital" value={`$${data.current_capital?.toFixed(2)}`} />
        <PnlDisplay pnl={data.total_pnl||0} pct={data.total_pnl_pct||0} />
        <Stat label="BTC" value={data.btc_price?`$${data.btc_price.toLocaleString()}`:"--"} color="#ffb800" size={14} />
        <Stat label="RSI" value={data.rsi?.toFixed(1)||"--"} color={data.rsi<30?"#00ff88":data.rsi>70?"#ff4444":"#ccc"} />
      </div>

      {/* Posición abierta */}
      {openTrade && (
        <div style={{ background:`${posColor}11`, border:`1px solid ${posColor}33`, borderRadius:10, padding:"12px 16px", marginBottom:14, display:"flex", justifyContent:"space-between", alignItems:"center" }}>
          <div>
            <span style={{ color:posColor, fontWeight:700, fontFamily:"monospace" }}>{openTrade.side}</span>
            <span style={{ color:"#666", fontSize:12, marginLeft:10 }}>entrada ${openTrade.entry_price?.toLocaleString()}</span>
            <div style={{ color:"#555", fontSize:11, marginTop:4 }}>{openTrade.reasoning?.slice(0,60)}...</div>
          </div>
          {unrealizedPct && (
            <div style={{ textAlign:"right" }}>
              <div style={{ color:"#555", fontSize:10 }}>P&L no realizado</div>
              <div style={{ color:parseFloat(unrealizedPct)>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{parseFloat(unrealizedPct)>=0?"+":""}{unrealizedPct}%</div>
            </div>
          )}
        </div>
      )}

      {/* Indicadores */}
      <div style={{ display:"flex", gap:6, marginBottom:14, flexWrap:"wrap" }}>
        <Badge text={`Trend: ${data.trend||"--"}`} color={data.trend==="bullish"?"#00ff88":data.trend==="bearish"?"#ff4444":"#666"} />
        <Badge text={`MACD: ${data.macd_cross||"--"}`} color={data.macd_cross==="bullish"?"#00ff88":data.macd_cross==="bearish"?"#ff4444":"#666"} />
        <Badge text={`Vol: ${data.vol_ratio?.toFixed(1)||"--"}x`} color={data.vol_ratio>1.5?"#ffcc00":"#666"} />
        <Badge text={`Funding: ${data.funding_rate>=0?"+":""}${data.funding_rate?.toFixed(4)||"0"}%`} color={Math.abs(data.funding_rate||0)>0.01?"#ff8c00":"#666"} />
      </div>

      <div style={{ display:"flex", gap:6, marginBottom:14 }}>{T("position","POSICIÓN")}{T("trades","TRADES")}{T("stats","STATS")}{T("log","LOG")}</div>

      {tab==="position" && (
        <div style={{ background:"rgba(0,0,0,0.2)", borderRadius:10, padding:14 }}>
          {!openTrade
            ? <div style={{ color:"#444", textAlign:"center", padding:20 }}>Sin posición — Claude esperando señal</div>
            : <div>
                <div style={{ display:"flex", gap:16, fontFamily:"monospace", fontSize:12, flexWrap:"wrap" }}>
                  <span style={{ color:"#666" }}>size <span style={{ color:"#ffb800" }}>${openTrade.size?.toFixed(2)}</span></span>
                  <span style={{ color:"#666" }}>SL <span style={{ color:"#ff4444" }}>${openTrade.stop_loss?.toLocaleString()}</span></span>
                  <span style={{ color:"#666" }}>TP <span style={{ color:"#00ff88" }}>${openTrade.take_profit?.toLocaleString()}</span></span>
                  <Badge text={openTrade.confidence} color={openTrade.confidence==="HIGH"?"#00ff88":"#ffcc00"} />
                </div>
              </div>
          }
        </div>
      )}

      {tab==="trades" && (
        <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
          {closed.length===0
            ? <div style={{ color:"#444", textAlign:"center", padding:24 }}>Sin trades cerrados aún</div>
            : [...closed].reverse().slice(0,10).map((t,i)=>(
              <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"10px 14px", display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                  <Badge text={t.side} color={t.side==="LONG"?"#00ff88":"#ff4444"} />
                  <Badge text={t.exit_reason||"--"} color="#555" />
                  <span style={{ color:"#555", fontSize:11 }}>${t.entry_price?.toLocaleString()} → ${t.exit_price?.toLocaleString()}</span>
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
            { label:"Capital inicial", value:`$${data.initial_capital?.toFixed(2)}` },
            { label:"Capital actual", value:`$${data.current_capital?.toFixed(2)}` },
            { label:"P&L total", value:`${(data.total_pnl||0)>=0?"+":""}$${Math.abs(data.total_pnl||0).toFixed(2)}`, color:(data.total_pnl||0)>=0?"#00ff88":"#ff4444" },
            { label:"Win rate", value:`${data.win_rate?.toFixed(1)}%`, color:"#ffcc00" },
            { label:"Max drawdown", value:`${data.max_drawdown?.toFixed(1)}%`, color:"#ff8c00" },
            { label:"Trades totales", value:closed.length + (openTrade?1:0) },
          ].map((s,i)=>(
            <div key={i} style={{ background:"rgba(255,255,255,0.02)", borderRadius:8, padding:"10px 14px" }}>
              <div style={{ color:"#555", fontSize:10, marginBottom:4 }}>{s.label}</div>
              <div style={{ color:s.color||"#ccc", fontFamily:"monospace", fontWeight:700 }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      {tab==="log" && (
        <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
          {(data.cycle_log||[]).slice(0,15).map((e,i)=>(
            <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
              <span style={{ color:"#555", minWidth:50 }}>{e.time}</span>
              <span style={{ color:e.msg?.includes("✓")?"#00ff88":e.msg?.includes("❌")?"#ff4444":e.msg?.includes("PAPER")?"#ffb800":"#888" }}>{e.msg}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── App Principal ─────────────────────────────────────────────────────────────


// ─── Panel Altcoins ────────────────────────────────────────────────────────────
function AltcoinPanel({ data }) {
  const [tab, setTab] = useState("positions");
  const T = (id, label, extra="") => (
    <button onClick={()=>setTab(id)} style={{ background:tab===id?"rgba(255,100,200,0.1)":"transparent", border:`1px solid ${tab===id?"#ff64c855":"transparent"}`, color:tab===id?"#ff64c8":"#555", borderRadius:7, padding:"5px 14px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>{label}{extra}</button>
  );
  const open = data.open_positions || [];
  const closed = data.closed_trades || [];
  const wins = closed.filter(t=>t.pnl>0);
  const scan = data.last_scan || [];
  const scanning = data.scanning || false;

  const stratColor = (s) => s==="MEAN_REVERSION"?"#00ff88":s==="MOMENTUM"?"#ffb800":s==="RANGE"?"#888bff":"#666";

  return (
    <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:16, padding:22 }}>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
        <div>
          <div style={{ color:"#ff64c8", fontWeight:700, letterSpacing:2, fontSize:13 }}>ALTCOINS — MULTI ESTRATEGIA</div>
          <div style={{ color:"#555", fontSize:11 }}>Top 20 por volumen · Claude elige estrategia · {data.leverage||3}x leverage</div>
        </div>
        <Badge text="PAPER" color="#ff64c8" />
      </div>

      <div style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)", flexWrap:"wrap", gap:12 }}>
        <Stat label="Capital" value={`$${(data.current_capital||0).toFixed(2)}`} />
        <PnlDisplay pnl={data.total_pnl||0} pct={data.total_pnl_pct||0} />
        <Stat label="Win Rate" value={`${(data.win_rate||0).toFixed(0)}%`} color="#ffcc00" />
        <Stat label="Abiertas" value={open.length} color="#ff64c8" size={20} />
        <Stat label="Trades" value={data.total_trades||0} color="#888" size={20} />
      </div>

      <div style={{ display:"flex", gap:6, marginBottom:14 }}>{T("positions","POSICIONES")}{T("scanner", scanning?"SCANNER ●":"SCANNER", scanning?" style={{animation:'pulse 1s infinite'}}":"")}{T("trades","TRADES")}{T("log","LOG")}</div>

      {tab==="positions" && (
        <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
          {open.length===0
            ? <div style={{ color:"#444", textAlign:"center", padding:24 }}>Sin posiciones abiertas — Claude escaneando mercado</div>
            : open.map((p,i)=>(
              <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.06)", borderRadius:10, padding:"12px 16px" }}>
                <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:6 }}>
                  <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                    <span style={{ color:"#ff64c8", fontWeight:700, fontFamily:"monospace" }}>{p.symbol}</span>
                    <Badge text={p.direction} color={p.direction==="LONG"?"#00ff88":"#ff4444"} />
                    <Badge text={p.strategy} color={stratColor(p.strategy)} />
                    <Badge text={p.confidence} color={p.confidence==="HIGH"?"#00ff88":"#ffcc00"} />
                  </div>
                  <span style={{ color:"#ffb800", fontFamily:"monospace" }}>${p.size_usdt?.toFixed(0)}</span>
                </div>
                <div style={{ color:"#555", fontSize:11 }}>{p.reasoning?.slice(0,80)}</div>
                <div style={{ display:"flex", gap:14, marginTop:6, fontFamily:"monospace", fontSize:11 }}>
                  <span style={{ color:"#666" }}>entrada <span style={{ color:"#ccc" }}>${p.entry_price?.toFixed(4)}</span></span>
                  <span style={{ color:"#666" }}>SL <span style={{ color:"#ff4444" }}>${p.stop_loss?.toFixed(4)}</span></span>
                  <span style={{ color:"#666" }}>TP <span style={{ color:"#00ff88" }}>${p.take_profit?.toFixed(4)}</span></span>
                </div>
              </div>
            ))
          }
        </div>
      )}

      {tab==="scanner" && (
        <div>
          <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:12 }}>
            {scanning && <div style={{ width:8, height:8, borderRadius:"50%", background:"#ff64c8", animation:"pulse 1s infinite" }} />}
            <span style={{ color:"#555", fontSize:11 }}>{scanning ? "Escaneando mercado con Claude..." : `Último scan: ${scan.length} altcoins analizadas`}</span>
          </div>
          <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
            {scan.length===0
              ? <div style={{ color:"#444", textAlign:"center", padding:24 }}>Esperando primer scan...</div>
              : scan.map((s,i)=>{
                  const skipIt = s.strategy==="SKIP" || s.direction==="SKIP" || s.confidence==="LOW";
                  const stratColor = s.strategy==="MEAN_REVERSION"?"#00ff88":s.strategy==="MOMENTUM"?"#ffb800":s.strategy==="RANGE"?"#888bff":"#444";
                  return (
                    <div key={i} style={{ background:skipIt?"rgba(255,255,255,0.01)":"rgba(255,255,255,0.03)", border:`1px solid ${skipIt?"rgba(255,255,255,0.04)":"rgba(255,255,255,0.08)"}`, borderRadius:8, padding:"10px 14px", opacity:skipIt?0.5:1 }}>
                      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:skipIt?0:6 }}>
                        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                          <span style={{ color:"#ff64c8", fontFamily:"monospace", fontWeight:700, minWidth:90 }}>{s.symbol}</span>
                          <Badge text={`RSI ${s.rsi?.toFixed(0)}`} color={s.rsi<30?"#00ff88":s.rsi>70?"#ff4444":"#666"} />
                          <Badge text={`Vol ${s.vol_ratio?.toFixed(1)}x`} color={s.vol_ratio>2?"#ffcc00":"#555"} />
                          <Badge text={s.trend} color={s.trend==="bullish"?"#00ff8855":"#ff444455"} />
                          <span style={{ color:"#444", fontSize:10 }}>{s.scanned_at}</span>
                        </div>
                        <div style={{ display:"flex", gap:6 }}>
                          {!skipIt && <Badge text={s.strategy} color={stratColor} />}
                          {!skipIt && <Badge text={s.direction} color={s.direction==="LONG"?"#00ff88":"#ff4444"} />}
                          {!skipIt && <Badge text={s.confidence} color={s.confidence==="HIGH"?"#00ff88":"#ffcc00"} />}
                          {skipIt && <Badge text="SKIP" color="#333" />}
                          {!skipIt && s.size_usdt>0 && <span style={{ color:"#ffb800", fontSize:11, fontFamily:"monospace" }}>${s.size_usdt?.toFixed(0)}</span>}
                        </div>
                      </div>
                      {!skipIt && s.reasoning && <div style={{ color:"#666", fontSize:11, fontStyle:"italic" }}>"{s.reasoning?.slice(0,100)}"</div>}
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
            ? <div style={{ color:"#444", textAlign:"center", padding:24 }}>Sin trades cerrados aún</div>
            : [...closed].reverse().slice(0,15).map((t,i)=>(
              <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"10px 14px", display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                  <span style={{ color:"#ff64c8", fontFamily:"monospace", minWidth:80 }}>{t.symbol}</span>
                  <Badge text={t.direction} color={t.direction==="LONG"?"#00ff88":"#ff4444"} />
                  <Badge text={t.strategy} color={stratColor(t.strategy)} />
                  <Badge text={t.exit_reason||"CLOSE"} color={t.exit_reason==="TAKE_PROFIT"?"#00ff88":t.exit_reason==="STOP_LOSS"?"#ff4444":"#666"} />
                </div>
                <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}</span>
              </div>
            ))
          }
        </div>
      )}

      {tab==="log" && (
        <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
          {(data.cycle_log||[]).slice(0,15).map((e,i)=>(
            <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
              <span style={{ color:"#555", minWidth:50 }}>{e.time}</span>
              <span style={{ color:e.msg?.includes("✓")?"#00ff88":e.msg?.includes("❌")?"#ff4444":"#888" }}>{e.msg}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Panel RSI S&P500 ──────────────────────────────────────────────────────────
function RsiPanel({ data }) {
  const [tab, setTab] = useState("position");
  const T = (id, label) => (
    <button onClick={()=>setTab(id)} style={{ background:tab===id?"rgba(100,180,255,0.1)":"transparent", border:`1px solid ${tab===id?"#64b4ff55":"transparent"}`, color:tab===id?"#64b4ff":"#555", borderRadius:7, padding:"5px 14px", fontSize:11, cursor:"pointer", fontFamily:"monospace" }}>{label}</button>
  );
  const closed = data.closed_trades || [];
  const wins = closed.filter(t=>t.pnl>0);
  const pos = data.current_position;
  const unrealPct = pos && data.entry_price ? "en curso" : null;

  return (
    <div style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.07)", borderRadius:16, padding:22 }}>
      {/* Header */}
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
        <div>
          <div style={{ color:"#64b4ff", fontWeight:700, letterSpacing:2, fontSize:13 }}>RSI MEAN REVERSION — S&P500</div>
          <div style={{ color:"#555", fontSize:11 }}>Basket: {(data.basket||[]).join(" · ")} · RSI{data.rsi_period||10} · paper trading</div>
        </div>
        <div style={{ display:"flex", gap:8, alignItems:"center" }}>
          <Badge text="PAPER" color="#64b4ff" />
          <Badge text="ALPACA" color="#555" />
        </div>
      </div>

      {/* Stats */}
      <div style={{ display:"flex", justifyContent:"space-between", padding:"14px 0", marginBottom:14, borderTop:"1px solid rgba(255,255,255,0.05)", borderBottom:"1px solid rgba(255,255,255,0.05)", flexWrap:"wrap", gap:12 }}>
        <Stat label="Capital" value={`$${(data.current_capital||0).toLocaleString()}`} />
        <PnlDisplay pnl={data.total_pnl||0} pct={data.total_pnl_pct||0} />
        <Stat label="Win Rate" value={`${(data.win_rate||0).toFixed(0)}%`} color="#ffcc00" />
        <Stat label="Trades" value={data.total_trades||0} color="#64b4ff" size={20} />
        <Stat label="Hoy" value={data.trades_today||0} color="#888" size={20} />
      </div>

      {/* Posición actual */}
      {pos && (
        <div style={{ background:"rgba(100,180,255,0.08)", border:"1px solid rgba(100,180,255,0.2)", borderRadius:10, padding:"12px 16px", marginBottom:14, display:"flex", justifyContent:"space-between", alignItems:"center" }}>
          <div>
            <span style={{ color:"#64b4ff", fontWeight:700, fontFamily:"monospace", fontSize:14 }}>{pos}</span>
            <span style={{ color:"#666", fontSize:12, marginLeft:10 }}>entrada ${data.entry_price?.toFixed(2)}</span>
            <span style={{ color:"#555", fontSize:11, marginLeft:10 }}>desde {data.entry_date}</span>
          </div>
          <div style={{ display:"flex", gap:8 }}>
            <Badge text={`TP +3%`} color="#00ff88" />
            <Badge text={`SL -3%`} color="#ff4444" />
          </div>
        </div>
      )}

      {/* Tabs */}
      <div style={{ display:"flex", gap:6, marginBottom:14 }}>{T("position","HOY")}{T("trades","TRADES")}{T("stats","STATS")}{T("log","LOG")}</div>

      {tab==="position" && (
        <div style={{ background:"rgba(0,0,0,0.2)", borderRadius:10, padding:16 }}>
          {!pos
            ? <div style={{ color:"#444", textAlign:"center", padding:20 }}>
                Sin posición abierta — el bot compra al cierre del mercado US (4PM ET)
              </div>
            : <div style={{ color:"#888", fontSize:13 }}>
                Posición abierta en <span style={{ color:"#64b4ff", fontWeight:700 }}>{pos}</span> — exit al cierre o si toca ±3%
              </div>
          }
        </div>
      )}

      {tab==="trades" && (
        <div>
          {/* Mini equity curve de texto */}
          {closed.length > 0 && (
            <div style={{ display:"flex", gap:4, marginBottom:12, flexWrap:"wrap" }}>
              {closed.slice(-20).map((t,i) => (
                <div key={i} title={`${t.symbol} ${t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}`} style={{ width:12, height:32, background:t.pnl>=0?"#00ff88":"#ff4444", borderRadius:2, opacity:0.7 + (i/closed.length*0.3) }} />
              ))}
            </div>
          )}
          <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
            {closed.length===0
              ? <div style={{ color:"#444", textAlign:"center", padding:24 }}>Sin trades cerrados aún</div>
              : [...closed].reverse().slice(0,12).map((t,i) => (
                <div key={i} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:8, padding:"10px 14px", display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                  <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                    <span style={{ color:"#64b4ff", fontFamily:"monospace", fontWeight:700, minWidth:44 }}>{t.symbol}</span>
                    <Badge text={t.exit_reason||"CLOSE"} color={t.exit_reason==="TAKE_PROFIT"?"#00ff88":t.exit_reason==="STOP_LOSS"?"#ff4444":"#666"} />
                    <span style={{ color:"#555", fontSize:11 }}>{t.date}</span>
                    <span style={{ color:"#444", fontSize:11 }}>RSI {t.rsi_entry}</span>
                  </div>
                  <span style={{ color:t.pnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontWeight:700 }}>{t.pnl>=0?"+":""}${t.pnl?.toFixed(2)}</span>
                </div>
              ))
            }
          </div>
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
              <div style={{ color:"#555", fontSize:10, marginBottom:4 }}>{s.label}</div>
              <div style={{ color:s.color||"#ccc", fontFamily:"monospace", fontWeight:700 }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      {tab==="log" && (
        <div style={{ background:"rgba(0,0,0,0.3)", borderRadius:8, padding:14, fontFamily:"monospace", fontSize:11 }}>
          {(data.cycle_log||[]).slice(0,15).map((e,i)=>(
            <div key={i} style={{ display:"flex", gap:10, marginBottom:5 }}>
              <span style={{ color:"#555", minWidth:50 }}>{e.time}</span>
              <span style={{ color:e.msg?.includes("✓")?"#00ff88":e.msg?.includes("❌")?"#ff4444":"#888" }}>{e.msg}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Dashboard() {
  const [bnData, setBnData] = useState(MOCK_BN);
  const [altData, setAltData] = useState(MOCK_ALT);
  const [rsiData, setRsiData] = useState(MOCK_RSI);
  const [blink, setBlink] = useState(true);
  const [time, setTime] = useState(new Date());
  const [lastFetch, setLastFetch] = useState("--");

  const fetchStates = useCallback(async () => {
    // Intenta leer los archivos JSON del paper trading via fetch
    // En desarrollo local con Vite, los archivos deben estar en /public/paper_trading/
    try {
      const alt = await fetch("/altcoin_data/state.json?t="+Date.now()).then(r=>r.json());
      setAltData(alt);
    } catch {}
    try {
      const bn = await fetch("/paper_trading/binance_state.json?t="+Date.now()).then(r=>r.json());
      setBnData(bn);
    } catch {}
    try {
      const rsi = await fetch("/rsi_bot_data/state.json?t="+Date.now()).then(r=>r.json());
      setRsiData(rsi);
    } catch {}
    setLastFetch(new Date().toLocaleTimeString("es-AR"));
  }, []);

  useEffect(() => {
    const t1 = setInterval(()=>setBlink(b=>!b), 800);
    const t2 = setInterval(()=>setTime(new Date()), 1000);
    const t3 = setInterval(fetchStates, 30000); // refresh cada 30s
    fetchStates();
    return ()=>{ clearInterval(t1); clearInterval(t2); clearInterval(t3); };
  }, [fetchStates]);

  const totalPnl = (bnData.total_pnl||0) + (altData.total_pnl||0) + (rsiData.total_pnl||0);
  const totalCapital = (bnData.current_capital||0) + (altData.current_capital||0) + (rsiData.current_capital||0);

  return (
    <div style={{ background:"#050508", minHeight:"100vh", color:"#ccc", fontFamily:"'Courier New', monospace", padding:"0 0 40px" }}>
      {/* Header */}
      <div style={{ borderBottom:"1px solid rgba(255,255,255,0.06)", padding:"14px 28px", display:"flex", alignItems:"center", justifyContent:"space-between", background:"rgba(255,255,255,0.01)" }}>
        <div style={{ display:"flex", alignItems:"center", gap:12 }}>
          <div style={{ width:9, height:9, borderRadius:"50%", background:"#00ff88", boxShadow:"0 0 8px #00ff88", opacity:blink?1:0.2, transition:"opacity 0.3s" }} />
          <span style={{ color:"#00ff88", fontWeight:700, letterSpacing:3, fontSize:13 }}>TRADING BOT HQ</span>
          <span style={{ color:"#333", fontSize:11 }}>paper trading</span>
        </div>
        <div style={{ display:"flex", gap:24, alignItems:"center" }}>
          <div style={{ textAlign:"center" }}>
            <div style={{ color:"#444", fontSize:10, marginBottom:2 }}>CAPITAL TOTAL</div>
            <div style={{ color:"#ccc", fontFamily:"monospace", fontSize:15, fontWeight:700 }}>${totalCapital.toFixed(2)}</div>
          </div>
          <div style={{ textAlign:"center" }}>
            <div style={{ color:"#444", fontSize:10, marginBottom:2 }}>P&L COMBINADO</div>
            <div style={{ color:totalPnl>=0?"#00ff88":"#ff4444", fontFamily:"monospace", fontSize:15, fontWeight:700 }}>{totalPnl>=0?"+":""}${totalPnl.toFixed(2)}</div>
          </div>
          <div style={{ textAlign:"right" }}>
            <div style={{ color:"#444", fontSize:10, marginBottom:2 }}>ACTUALIZADO</div>
            <div style={{ color:"#555", fontSize:11 }}>{lastFetch}</div>
          </div>
          <span style={{ color:"#444", fontSize:11 }}>{time.toLocaleTimeString("es-AR")}</span>
        </div>
      </div>

      <div style={{ padding:"24px 28px", display:"flex", gap:20, flexWrap:"wrap" }}>
        <BinancePanel data={bnData} />
      </div>

      <div style={{ padding:"0 28px 20px" }}>
        <AltcoinPanel data={altData} />
        <div style={{ height:20 }} />
        <RsiPanel data={rsiData} />
      </div>
      <div style={{ margin:"0 28px", padding:"12px 18px", background:"rgba(0,255,136,0.03)", border:"1px solid rgba(0,255,136,0.1)", borderRadius:10, fontSize:11, color:"#777" }}>
        <span style={{ color:"#00ff88" }}>● PAPER TRADING</span> — Datos reales, dinero simulado. El dashboard se actualiza automáticamente cada 30 segundos desde <code style={{ color:"#aaa" }}>paper_trading/*.json</code>
      </div>
    </div>
  );
}

import pandas as pd
import json
import os
from pathlib import Path

def analyze_performance():
    trades_file = Path("trades.jsonl")
    if not trades_file.exists():
        print("❌ No se encontró el archivo trades.jsonl")
        return

    trades = []
    with open(trades_file, "r") as f:
        for line in f:
            try:
                trades.append(json.loads(line))
            except:
                continue

    if not trades:
        print("⚠️ El archivo de trades está vacío.")
        return

    df = pd.DataFrame(trades)
    
    # Asegurar que las columnas existen
    if 'strategy' not in df.columns:
        df['strategy'] = 'UNKNOWN'
    if 'pnl' not in df.columns:
        print("❌ No hay datos de PnL para analizar.")
        return

    print("\n" + "="*50)
    print("📊 REPORTE DE DESEMPEÑO POR ESTRATEGIA")
    print("="*50)

    # Agrupar por estrategia
    stats = df.groupby('strategy').agg({
        'pnl': 'sum',
        'id': 'count'
    }).rename(columns={'id': 'total_trades'})

    # Win Rate
    wins = df[df['pnl'] > 0].groupby('strategy').size()
    stats['wins'] = wins
    stats['wins'] = stats['wins'].fillna(0)
    stats['win_rate'] = (stats['wins'] / stats['total_trades'] * 100).round(1)

    # PnL Promedio
    stats['avg_pnl'] = (stats['pnl'] / stats['total_trades']).round(2)

    # Ordenar por PnL total
    stats = stats.sort_values('pnl', ascending=False)
    
    print(stats[['total_trades', 'win_rate', 'pnl', 'avg_pnl']])

    print("\n" + "="*50)
    print("📈 TOP 5 MONEDAS MÁS RENTABLES")
    coin_stats = df.groupby('symbol')['pnl'].sum().sort_values(ascending=False).head(5)
    print(coin_stats)

    print("\n" + "="*50)
    print("📉 TOP 5 MONEDAS MENOS RENTABLES")
    coin_loss = df.groupby('symbol')['pnl'].sum().sort_values(ascending=True).head(5)
    print(coin_loss)
    print("="*50 + "\n")

if __name__ == "__main__":
    analyze_performance()

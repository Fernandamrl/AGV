"""
AGV Logistica -- API de Reports de SLA  (v10)
FastAPI que o n8n chama para gerar os reports do WhatsApp.

ENDPOINTS:
    GET /resumo      -> Report das 07h
    GET /painel      -> Operacional a cada 3h (08,11,14,17,20)
    GET /fechamento  -> Report das 18h
    GET /health      -> Healthcheck
"""

from fastapi import FastAPI
from datetime import datetime, timedelta, date
import unicodedata, ast, requests, pandas as pd, holidays

app = FastAPI(title="AGV SLA API")

# ─── CONFIGURACAO ─────────────────────────────────────────────────

API_URL   = "https://api-servicos.octalog.com.br/consulta/pedidos"
API_TOKEN = "MTEx26QUdWOjIwMjYtMDYtMjE6UGVkaWRvcy1CSQ=="

CIDADES_D2_NORM = {
    _s for _s in [
        'santos','sao jose dos campos','praia grande',
        'indaiatuba','guaruja','sao vicente','cubatao'
    ]
}

FERIADOS        = holidays.Brazil(state='SP', years=range(2025, 2028))
FERIADOS_EXTRAS = { date(2026, 6, 4) }
CEP_SP_MIN, CEP_SP_MAX = 1000000, 9999999


# ─── UTILITARIOS ──────────────────────────────────────────────────

def sem_acento(s):
    return ''.join(
        c for c in unicodedata.normalize('NFD', str(s))
        if unicodedata.category(c) != 'Mn'
    ).lower().strip()

def is_util(d):
    if isinstance(d, datetime): d = d.date()
    return d.weekday() < 5 and d not in FERIADOS and d not in FERIADOS_EXTRAS

def prox_util(d):
    if isinstance(d, datetime): d = d.date()
    while not is_util(d): d += timedelta(days=1)
    return d

def add_uteis(d, n):
    if isinstance(d, datetime): d = d.date()
    c = 0
    while c < n:
        d += timedelta(days=1)
        if is_util(d): c += 1
    return d

def parse_dict(v):
    if isinstance(v, dict): return v
    if isinstance(v, str) and v.strip().startswith('{'):
        try: return ast.literal_eval(v.strip())
        except: pass
    return {}

def to_date_br(v):
    if not v or pd.isna(v): return None
    try:
        return pd.to_datetime(v, utc=True).tz_convert('America/Sao_Paulo').date()
    except: return None

def now_brt():
    return pd.Timestamp.now('America/Sao_Paulo')


# ─── FETCH ────────────────────────────────────────────────────────

def fetch(data_inicio: str, data_fim: str) -> pd.DataFrame:
    r = requests.get(
        API_URL,
        headers={"token": API_TOKEN},
        params={"DataInicio": data_inicio, "DataFinal": data_fim},
        timeout=120
    )
    r.raise_for_status()
    raw = r.json()
    pedidos = raw if isinstance(raw, list) else next(
        (raw.get(k) for k in ('pedidos','data','result') if raw.get(k)),
        next((v for v in raw.values() if isinstance(v, list)), []) if isinstance(raw, dict) else []
    )
    if not pedidos: return pd.DataFrame()
    df = pd.DataFrame(pedidos)

    for col in df.columns:
        if df[col].apply(lambda x: isinstance(x, (dict, list))).any():
            parsed = df[col].apply(parse_dict)
            if parsed.apply(bool).any():
                if col == 'Loja':
                    df['LojaNome']  = parsed.apply(lambda x: x.get('Loja',''))
                    df['LojaGrupo'] = parsed.apply(lambda x: x.get('Grupo',''))
                elif col == 'Cliente':
                    df['CidadeDestino'] = parsed.apply(lambda x: x.get('ClienteCidade',''))
                    df['CEP']           = parsed.apply(lambda x: x.get('ClienteCep',''))
                    df['ClienteNome']   = parsed.apply(lambda x: x.get('Cliente',''))
            df[col] = df[col].apply(lambda x: str(x) if isinstance(x, (dict,list)) else x)

    rename = {'Id':'ID','DataSla':'DataSLA_Sistema','SLANoPrazo':'SLANoPrazo_Sistema'}
    df = df.rename(columns={k:v for k,v in rename.items() if k in df.columns})

    for col in ['CidadeDestino','CEP','LojaNome','LojaGrupo','Polo','DataConclusao','DataSLA_Sistema']:
        if col not in df.columns: df[col] = ''

    df['CidadeDestino'] = df['CidadeDestino'].str.strip().str.title()

    def calc(row):
        s = str(row.get('Servico','') or '').upper()
        p = str(row.get('Prazo','') or '').upper()
        cidade = sem_acento(row.get('CidadeDestino','') or '')
        cep    = str(row.get('CEP','') or '').replace('-','').replace('.','').strip()
        integ  = row.get('DataIntegracao')

        if '1H' in s or '1H' in p:
            if pd.notna(integ):
                return (pd.to_datetime(integ,utc=True)+timedelta(hours=1)).date(),'1Hr'
            return None,'1Hr'
        if pd.isna(integ) or str(integ)=='': return None,'SEM_DATA'

        dt = pd.to_datetime(integ,utc=True).tz_convert('America/Sao_Paulo').replace(tzinfo=None)
        d0 = prox_util(dt.date()) if dt.hour < 8 else prox_util(dt.date()+timedelta(days=1))

        if cidade in CIDADES_D2_NORM:
            dias,tipo = 2,'D+2 (cidade)'
        else:
            try: cep_n = int(cep[:8].ljust(8,'0')) if cep else 0
            except: cep_n = 0
            dias,tipo = 1,('D+1 (Grande SP)' if CEP_SP_MIN<=cep_n<=CEP_SP_MAX else 'D+1 (padrao)')

        return add_uteis(d0,dias), tipo

    res = df.apply(calc, axis=1)
    df['SLA_calc'] = res.apply(lambda x: x[0])
    df['Tipo_SLA'] = res.apply(lambda x: x[1])

    def status(row):
        c   = row.get('DataConclusao')
        sst = row.get('DataSLA_Sistema')
        if not c or pd.isna(c) or str(c)=='' or not sst or pd.isna(sst) or str(sst)=='': return 'Pendente'
        try:
            return 'No Prazo' if to_date_br(c) <= pd.to_datetime(sst).date() else 'Atrasado'
        except: return 'Pendente'

    df['Status_SLA'] = df.apply(status, axis=1)

    def diff(row):
        c = row.get('SLA_calc')
        s = row.get('DataSLA_Sistema')
        if c is None or not s or pd.isna(s): return None
        try: return (pd.to_datetime(s).date() - c).days
        except: return None

    df['Diff_SLA_dias'] = df.apply(diff, axis=1)
    return df


# ─── KPIs ─────────────────────────────────────────────────────────

def kpis(df: pd.DataFrame) -> dict:
    sem1h = df[~df['Tipo_SLA'].str.contains('1Hr', na=False)]
    ent   = sem1h[sem1h['Status_SLA'] != 'Pendente']
    n  = len(ent)
    ok = int((ent['Status_SLA']=='No Prazo').sum())
    at = n - ok
    pend = int((sem1h['Status_SLA']=='Pendente').sum())
    sla  = ok/n*100 if n else 0

    # Por polo: ok (no prazo), atr (atrasados), pen (pendentes)
    polos = {}
    if 'Polo' in sem1h.columns:
        for polo, g in sem1h[sem1h['Polo'].str.strip().ne('')].groupby('Polo'):
            total = len(g)
            g_ent = g[g['Status_SLA'] != 'Pendente']
            ne    = len(g_ent)
            oe    = int((g_ent['Status_SLA']=='No Prazo').sum())
            atr_n = ne - oe
            pen_n = total - ne
            if ne == 0: continue
            polos[polo] = {
                'total': total, 'entregues': ne,
                'ok': oe, 'atr': atr_n, 'pen': pen_n,
                'sla': oe/ne*100
            }

    return {
        'total': len(df), 'sem1h': len(sem1h), 'entregues': n,
        'ok': ok, 'atrasados': at, 'pendentes': pend, 'sla': sla,
        'polos': polos,
    }


# ─── STATUS MAP ───────────────────────────────────────────────────

STATUS_MAP = {
    'Entrega Realizada':                    'Entregue',
    'Chegada no Destinatario':              'Entregue',
    'Chegada no Destinatario':              'Entregue',
    'Coletado na Loja':                     'Entregue',
    'Despachado':                           'Em Transito',
    'Chegada na Base':                      'Em Transito',
    'Transferencia entre unidades':         'Em Transito',
    'Rota de Entrega':                      'Em Transito',
    'Integracao Recebida':                  'Expedindo',
    'Nao Coletado':                         'Expedindo',
    'Coleta Cancelado pelo Remetente':      'Cancelado',
    'Entrega Cancelada pelo Remetente':     'Cancelado',
    'Cancelado pelo Destinatario':          'Cancelado',
    'Cliente Ausente':                      'Insucesso',
    'Localidade Nao Atendida':              'Insucesso',
    'Endereco Nao Localizado':              'Insucesso',
    'Numero nao Localizado':                'Insucesso',
    'Estabelecimento Fechado':              'Insucesso',
    'Destinatario Nao Encontrado':          'Insucesso',
    'Recusado por Terceiro':                'Insucesso',
    'Outras Ocorrencias':                   'Insucesso',
    'Em Devolucao':                         'Devolucao',
    'Devolvido':                            'Devolucao',
    'Pedido Extraviado':                    'Problema',
    'Pedido Danificado':                    'Problema',
    'Aguardando Tratativa':                 'Problema',
    # Com acentos
    'Acareacao: Entrega Realizada':         'Entregue',
    'Chegada no Destinatario':              'Entregue',
    'Transferencia entre unidades':         'Em Transito',
    'Integracao Recebida':                  'Expedindo',
    'Nao Coletado':                         'Expedindo',
    'Cancelado pelo Destinatario':          'Cancelado',
    'Localidade Nao Atendida':              'Insucesso',
    'Endereco Nao Localizado':              'Insucesso',
    'Numero nao Localizado':                'Insucesso',
    'Destinatario Nao Encontrado':          'Insucesso',
    'Outras Ocorrencias':                   'Insucesso',
    'Em Devolucao':                         'Devolucao',
}


def _apply_status_map(series):
    """Aplica STATUS_MAP e tenta sem acento como fallback."""
    result = series.map(STATUS_MAP)
    mask   = result.isna()
    if mask.any():
        result[mask] = series[mask].apply(
            lambda x: STATUS_MAP.get(
                ''.join(c for c in unicodedata.normalize('NFD', str(x))
                        if unicodedata.category(c) != 'Mn'), 'Outros'
            )
        )
    return result.fillna('Outros')


# ─── STATUS OPERACIONAL ───────────────────────────────────────────

def status_operacional(df: pd.DataFrame) -> dict:
    sem1h = df[~df['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h['StatusOp'] = _apply_status_map(sem1h['Status'])
    c = sem1h['StatusOp'].value_counts().to_dict()

    lojas_criticas = []
    if 'LojaNome' in sem1h.columns:
        for loja, g in sem1h[sem1h['LojaNome'].str.strip().ne('')].groupby('LojaNome'):
            t = len(g); ins = (g['StatusOp']=='Insucesso').sum()
            if t >= 20 and ins/t > 0.10:
                lojas_criticas.append({'loja':loja,'total':t,'insucesso':int(ins),'pct':ins/t*100})
    lojas_criticas.sort(key=lambda x: -x['pct'])

    # Por grupo/contratante: ok / atr / pen (mesmo criterio que polo)
    grupos = {}
    if 'LojaGrupo' in sem1h.columns:
        for grp, g in sem1h[sem1h['LojaGrupo'].str.strip().ne('')].groupby('LojaGrupo'):
            total = len(g)
            ok    = int((g['Status_SLA']=='No Prazo').sum())
            atr   = int((g['Status_SLA']=='Atrasado').sum())
            pen   = int((g['Status_SLA']=='Pendente').sum())
            ent   = ok + atr
            ins   = int((g['StatusOp']=='Insucesso').sum())
            sla   = ok/ent*100 if ent else 0
            grupos[grp] = {
                'total': total, 'ok': ok, 'atr': atr, 'pen': pen,
                'entregue': ent, 'insucesso': ins,
                'pct_ent': sla, 'pct_ins': ins/total*100
            }

    return {'contagens': c, 'lojas_criticas': lojas_criticas, 'grupos': grupos}


# ─── FORMATACAO ───────────────────────────────────────────────────

def emoji_circle(pct):
    if pct >= 95: return '🟢'
    if pct >= 88: return '🟡'
    return '🔴'

def fmt_polo(polos: dict, top=7) -> str:
    """83% | 2781 ped = 1867 Ok / 329 ATR / 585 PEN"""
    if not polos: return '_Sem dados de polo_'
    ordenados = sorted(polos.items(), key=lambda x: x[1]['total'], reverse=True)[:top]
    linhas = []
    for nome, d in ordenados:
        e = emoji_circle(d['sla'])
        linhas.append(
            f"{e} *{nome}*: {d['sla']:.0f}% | {d['total']} ped"
            f" = {d['ok']} Ok / {d['atr']} ATR / {d['pen']} PEN"
        )
    return '\n'.join(linhas)

def fmt_polo_resumo(polos: dict, top=5) -> str:
    """Para /resumo: sla% | ok ok / total ped"""
    if not polos: return '_Sem dados de polo_'
    ordenados = sorted(polos.items(), key=lambda x: x[1]['entregues'], reverse=True)[:top]
    linhas = []
    for nome, d in ordenados:
        e = emoji_circle(d['sla'])
        linhas.append(f"{e} *{nome}*: {d['sla']:.0f}% | {d['ok']} ok / {d['total']} ped")
    return '\n'.join(linhas)

def fmt_status_grupos(grupos: dict) -> str:
    """83% | 4399 ped = 3646 Ok / 753 ATR / X PEN"""
    if not grupos: return '_Sem dados_'
    linhas = []
    for grp, d in sorted(grupos.items(), key=lambda x: -x[1]['total']):
        nome = grp.replace('GRUPO ','').replace(' DROGASIL','').title()[:25]
        e    = emoji_circle(d['pct_ent'])
        linhas.append(
            f"{e} *{nome}*: {d['pct_ent']:.0f}% | {d['total']} ped"
            f" = {d['ok']} Ok / {d['atr']} ATR / {d['pen']} PEN"
        )
    return '\n'.join(linhas)

def fmt_lojas_criticas(lojas) -> str:
    if not lojas: return ''
    linhas = ['*Lojas com insucesso >10%:*']
    for l in lojas[:5]:
        linhas.append(f"  {l['loja'][:28]}: {l['pct']:.0f}% ({l['insucesso']}/{l['total']})")
    return '\n'.join(linhas)


# ─── ENDPOINTS ────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "hora": now_brt().strftime('%d/%m/%Y %H:%M')}


@app.get("/resumo")
def resumo():
    hoje   = date.today()
    ontem  = (hoje - timedelta(days=1)).strftime('%Y-%m-%d')
    mes_s  = hoje.replace(day=1).strftime('%Y-%m-%d')
    hoje_s = hoje.strftime('%Y-%m-%d')
    ts       = now_brt()
    data_fmt = ts.strftime('%d/%m/%Y')
    hora     = ts.strftime('%H:%M')

    df_on  = fetch(ontem, ontem)
    df_mes = fetch(mes_s, hoje_s)
    if df_on.empty:
        return {"mensagem": f"Sem dados para {ontem}"}

    k_on  = kpis(df_on)
    k_mes = kpis(df_mes) if not df_mes.empty else k_on

    e_on  = emoji_circle(k_on['sla'])
    e_mes = emoji_circle(k_mes['sla'])

    msg = (
        f"🌅 *RESUMO AGV -- {data_fmt} | {hora}*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📦 *Ontem ({ontem})*\n"
        f"• Pedidos D+: {k_on['sem1h']}\n"
        f"• Entregues: {k_on['entregues']}\n"
        f"• No Prazo: {k_on['ok']} | Atrasados: {k_on['atrasados']}\n"
        f"• SLA: {e_on} *{k_on['sla']:.1f}%*\n"
        f"\n"
        f"📅 *Acumulado do mes*\n"
        f"• Total D+: {k_mes['sem1h']}\n"
        f"• SLA mes: {e_mes} *{k_mes['sla']:.1f}%*\n"
        f"• Atrasados mes: {k_mes['atrasados']}\n"
        f"\n"
        f"🏢 *SLA por Polo (ontem)*\n"
        f"{fmt_polo_resumo(k_on['polos'])}\n"
        f"━━━━━━━━━━━━━━━━━"
    )
    return {"mensagem": msg.strip()}


@app.get("/painel")
def painel():
    hoje       = date.today()
    inicio_10d = (hoje - timedelta(days=10)).strftime('%Y-%m-%d')
    mes_s      = hoje.replace(day=1).strftime('%Y-%m-%d')
    hoje_s     = hoje.strftime('%Y-%m-%d')
    ts         = now_brt()
    hoje_fmt   = hoje.strftime('%d/%m/%Y')
    hora       = ts.strftime('%H:%M')

    df10 = fetch(inicio_10d, hoje_s)
    if df10.empty:
        return {"mensagem": f"Sem dados ({hoje_s})"}

    sem1h_10 = df10[~df10['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h_10['StatusOp'] = _apply_status_map(sem1h_10['Status'])
    sem1h_10['DataInteg_d'] = (
        pd.to_datetime(sem1h_10['DataIntegracao'], utc=True, errors='coerce')
        .dt.tz_convert('America/Sao_Paulo').dt.date
    )
    hoje_df = sem1h_10[sem1h_10['DataInteg_d'] == hoje]
    c = hoje_df['StatusOp'].value_counts().to_dict()

    df_mes = pd.DataFrame()
    try:
        df_mes = fetch(mes_s, hoje_s)
    except Exception:
        try:
            df_mes = fetch((hoje - timedelta(days=20)).strftime('%Y-%m-%d'), hoje_s)
        except Exception:
            df_mes = df10

    sem1h_mes = df_mes[~df_mes['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h_mes['StatusOp']  = _apply_status_map(sem1h_mes['Status'])
    sem1h_mes['DataSLA_d'] = pd.to_datetime(sem1h_mes['DataSLA_Sistema'], errors='coerce').dt.date

    abertos_mes   = sem1h_mes[~sem1h_mes['StatusOp'].isin(['Entregue','Cancelado','Devolucao'])].copy()
    atrasados_mes = abertos_mes[abertos_mes['DataSLA_d'].apply(lambda x: pd.notna(x) and x < hoje)]
    n_atrasados   = len(atrasados_mes)

    linhas_cont = []
    if not atrasados_mes.empty:
        col = 'LojaGrupo' if 'LojaGrupo' in atrasados_mes.columns else 'LojaNome'
        for nome, cnt in atrasados_mes[col].value_counts().items():
            if not nome or str(nome).strip() == '': continue
            nome_c = str(nome).replace('GRUPO ','').replace(' DROGASIL','').title()[:30]
            linhas_cont.append(f"  🔴 {nome_c}: {cnt} atrasados")
    cont_fmt = '\n'.join(linhas_cont) if linhas_cont else '  Nenhum atrasado'

    linhas_polo = []
    if not atrasados_mes.empty and 'Polo' in atrasados_mes.columns:
        for polo, cnt in atrasados_mes[atrasados_mes['Polo'].str.strip().ne('')]['Polo'].value_counts().items():
            linhas_polo.append(f"  🔴 {polo}: {cnt} atrasados")
    polo_fmt = '\n'.join(linhas_polo) if linhas_polo else '  Nenhum atrasado'

    msg = (
        f"⚙️ *PAINEL -- {hoje_fmt} | {hora}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 *Status operacional (dia)*\n"
        f"• ✅ Entregues: {c.get('Entregue', 0)}\n"
        f"• 🚚 Em Transito: {c.get('Em Transito', 0)}\n"
        f"• 📬 Expedindo: {c.get('Expedindo', 0)}\n"
        f"• 🔄 Insucesso: {c.get('Insucesso', 0)}\n"
        f"• ↩️ Devolucao: {c.get('Devolucao', 0)}\n"
        f"• ❌ Cancelado: {c.get('Cancelado', 0)}\n"
        f"• ⌛ Atrasados: {n_atrasados}\n"
        f"\n"
        f"📋 *Por Contratante (atrasados -- mes)*\n"
        f"{cont_fmt}\n"
        f"\n"
        f"🏢 *Por Polo (atrasados -- mes)*\n"
        f"{polo_fmt}\n"
        f"━━━━━━━━━━━━━━━━"
    )
    return {"mensagem": msg.strip()}


@app.get("/fechamento")
def fechamento():
    hoje   = date.today()
    hoje_s = hoje.strftime('%Y-%m-%d')
    mes_s  = hoje.replace(day=1).strftime('%Y-%m-%d')
    ts       = now_brt()
    data_fmt = ts.strftime('%d/%m/%Y')
    hora     = ts.strftime('%H:%M')

    df_hj  = fetch(hoje_s, hoje_s)
    df_mes = fetch(mes_s, hoje_s)
    if df_hj.empty:
        return {"mensagem": f"Sem dados para fechamento de {hoje_s}"}

    k_hj  = kpis(df_hj)
    k_mes = kpis(df_mes) if not df_mes.empty else k_hj

    st = status_operacional(df_mes) if not df_mes.empty else {}
    c  = st.get('contagens', {})
    lojas_alert = fmt_lojas_criticas(st.get('lojas_criticas', []))
    grupos_fmt  = fmt_status_grupos(st.get('grupos', {}))
    polos_fmt   = fmt_polo(k_mes['polos'], top=7)

    e_hj  = emoji_circle(k_hj['sla'])
    e_mes = emoji_circle(k_mes['sla'])
    lojas_section = ('\n' + lojas_alert) if lojas_alert else ''

    msg = (
        f"🌆 *FECHAMENTO AGV -- {data_fmt} | {hora}*\n"
        f"━━━━━━━━━━━\n"
        f"📦 *Dia de hoje*\n"
        f"• Integrados: {k_hj['total']} | D+: {k_hj['sem1h']}\n"
        f"• Entregues: {k_hj['entregues']} | Pendentes: {k_hj['pendentes']}\n"
        f"• Atrasados: {k_hj['atrasados']}\n"
        f"• SLA do dia: {e_hj} *{k_hj['sla']:.1f}%*\n"
        f"\n"
        f"📅 *Acumulado do mes*\n"
        f"• Total D+: {k_mes['sem1h']}\n"
        f"• Entregues: {k_mes['entregues']}\n"
        f"• SLA mes: {e_mes} *{k_mes['sla']:.1f}%*\n"
        f"• Atrasados: {k_mes['atrasados']}\n"
        f"\n"
        f"📋 *Status operacional (mes)*\n"
        f"• ✅ Entregues: {c.get('Entregue', 0)}\n"
        f"• 🚚 Em Transito: {c.get('Em Transito', 0)}\n"
        f"• 📬 Expedindo: {c.get('Expedindo', 0)}\n"
        f"• 🔄 Insucesso: {c.get('Insucesso', 0)}\n"
        f"• ↩️ Devolucao: {c.get('Devolucao', 0)}\n"
        f"• ❌ Cancelado: {c.get('Cancelado', 0)}\n"
        f"• ⌛ Atrasados: {k_mes['atrasados']}\n"
         f"\n"
        f"🏢 *Polos SLA mes*\n"
        f"{polos_fmt}\n"
        f"\n"
        f"📊 *Por Contratante (mes)*\n"
        f"{grupos_fmt}"
        f"{lojas_section}\n"
        f"━━━━━━━━━━━━━━"
    )
    return {"mensagem": msg.strip()}


# ─── START ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

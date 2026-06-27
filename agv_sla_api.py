"""
AGV Logistica -- API de Reports de SLA  (v13)
FastAPI que o n8n chama para gerar os reports do WhatsApp.

Mudancas v13:
- fetch_chunked(): divide janelas grandes em chunks de 3 dias para evitar timeout
- Painel usa fetch_chunked para df10 (10 dias) -- resolve "Painel indisponivel"
- Status operacional agora mostra abertos_mes (todos em aberto), nao so hoje
"""
from fastapi import FastAPI
from datetime import datetime, timedelta, date
import unicodedata, ast, requests, pandas as pd, holidays

app = FastAPI(title="AGV SLA API")

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

def fetch(data_inicio: str, data_fim: str) -> pd.DataFrame:
    try:
        r = requests.get(
            API_URL,
            headers={"token": API_TOKEN},
            params={"DataInicio": data_inicio, "DataFinal": data_fim},
            timeout=120
        )
        r.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"[TIMEOUT] fetch({data_inicio}, {data_fim})")
        return pd.DataFrame()
    except requests.exceptions.RequestException as e:
        print(f"[ERRO] fetch({data_inicio}, {data_fim}) -- {e}")
        return pd.DataFrame()

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

def fetch_chunked(data_inicio: str, data_fim: str, chunk_days: int = 3) -> pd.DataFrame:
    """Divide janelas grandes em chunks menores para evitar timeout da API."""
    start  = date.fromisoformat(data_inicio)
    end    = date.fromisoformat(data_fim)
    frames = []
    cur    = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        df = fetch(cur.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d'))
        if not df.empty:
            frames.append(df)
        cur = chunk_end + timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def kpis(df: pd.DataFrame) -> dict:
    sem1h = df[~df['Tipo_SLA'].str.contains('1Hr', na=False)]
    ent   = sem1h[sem1h['Status_SLA'] != 'Pendente']
    n  = len(ent)
    ok = int((ent['Status_SLA']=='No Prazo').sum())
    at = n - ok
    pend = int((sem1h['Status_SLA']=='Pendente').sum())
    sla  = ok/n*100 if n else 0
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

STATUS_MAP = {
    'Entrega Realizada': 'Entregue', 'Chegada no Destinatario': 'Entregue',
    'Coletado na Loja': 'Entregue', 'Acareacao: Entrega Realizada': 'Entregue',
    'Despachado': 'Em Transito', 'Chegada na Base': 'Em Transito',
    'Transferencia entre unidades': 'Em Transito', 'Rota de Entrega': 'Em Transito',
    'Integracao Recebida': 'Expedindo', 'Nao Coletado': 'Expedindo',
    'Coleta Cancelado pelo Remetente': 'Cancelado',
    'Entrega Cancelada pelo Remetente': 'Cancelado',
    'Cancelado pelo Destinatario': 'Cancelado',
    'Cliente Ausente': 'Insucesso', 'Localidade Nao Atendida': 'Insucesso',
    'Endereco Nao Localizado': 'Insucesso', 'Numero nao Localizado': 'Insucesso',
    'Estabelecimento Fechado': 'Insucesso', 'Destinatario Nao Encontrado': 'Insucesso',
    'Recusado por Terceiro': 'Insucesso', 'Outras Ocorrencias': 'Insucesso',
    'Em Devolucao': 'Devolucao', 'Devolvido': 'Devolucao',
    'Pedido Extraviado': 'Problema', 'Pedido Danificado': 'Problema',
    'Aguardando Tratativa': 'Problema',
}

def _apply_status_map(series):
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

def emoji_circle(pct):
    if pct >= 95: return '\U0001f7e2'
    if pct >= 88: return '\U0001f7e1'
    return '\U0001f534'

def fmt_polo(polos: dict, top=7) -> str:
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
    if not polos: return '_Sem dados de polo_'
    ordenados = sorted(polos.items(), key=lambda x: x[1]['entregues'], reverse=True)[:top]
    linhas = []
    for nome, d in ordenados:
        e = emoji_circle(d['sla'])
        linhas.append(f"{e} *{nome}*: {d['sla']:.0f}% | {d['ok']} ok / {d['total']} ped")
    return '\n'.join(linhas)

def fmt_status_grupos(grupos: dict) -> str:
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

@app.get("/health")
def health():
    return {"status": "ok", "hora": now_brt().strftime('%d/%m/%Y %H:%M')}

@app.get("/resumo")
def resumo():
    hoje       = date.today()
    ontem      = hoje - timedelta(days=1)
    inicio_10d = (hoje - timedelta(days=10)).strftime('%Y-%m-%d')
    mes_s      = hoje.replace(day=1).strftime('%Y-%m-%d')
    hoje_s     = hoje.strftime('%Y-%m-%d')
    ts         = now_brt()
    data_fmt   = ts.strftime('%d/%m/%Y')
    hora       = ts.strftime('%H:%M')
    ontem_fmt  = ontem.strftime('%d/%m')
    hoje_fmt_r = hoje.strftime('%d/%m')

    # fetch_chunked cobre 10 dias -- inclui pedidos D+1 e D+2 dos dias anteriores
    df10 = fetch_chunked(inicio_10d, hoje_s, chunk_days=3)
    if df10.empty:
        return {"mensagem": f"_Resumo indisponivel ({data_fmt} | {hora}) -- API sem resposta_"}

    sem1h = df10[~df10['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h['StatusOp']  = _apply_status_map(sem1h['Status'])
    sem1h['DataSLA_d'] = pd.to_datetime(sem1h['DataSLA_Sistema'], errors='coerce').dt.date

    # Ontem: pedidos cujo PRAZO (DataSLA) era ontem
    on_df      = sem1h[sem1h['DataSLA_d'] == ontem]
    on_ok      = int((on_df['Status_SLA'] == 'No Prazo').sum())
    on_atr_ent = int((on_df['Status_SLA'] == 'Atrasado').sum())
    on_nao_ent = int((on_df['Status_SLA'] == 'Pendente').sum())
    on_total   = len(on_df)
    on_sla     = on_ok / (on_ok + on_atr_ent) * 100 if (on_ok + on_atr_ent) > 0 else 0

    # Hoje: pedidos cujo PRAZO e hoje, breakdown dos pendentes por StatusOp
    hj_df        = sem1h[sem1h['DataSLA_d'] == hoje]
    hj_total     = len(hj_df)
    hj_ok        = int((hj_df['Status_SLA'] == 'No Prazo').sum())
    hj_atr       = int((hj_df['Status_SLA'] == 'Atrasado').sum())
    hj_pend_df   = hj_df[hj_df['Status_SLA'] == 'Pendente']
    hj_pend      = len(hj_pend_df)
    hj_transito  = int((hj_pend_df['StatusOp'] == 'Em Transito').sum())
    hj_expedindo = int((hj_pend_df['StatusOp'] == 'Expedindo').sum())
    hj_insucesso = int((hj_pend_df['StatusOp'] == 'Insucesso').sum())

    # Mes acumulado (tenta fetch unico; fallback ja tratado acima via df10)
    df_mes  = fetch(mes_s, hoje_s)
    av      = ' _(parcial)_' if df_mes.empty else ''
    k_mes   = kpis(df_mes) if not df_mes.empty else {}
    e_mes   = emoji_circle(k_mes['sla']) if k_mes else '\U0001f534'
    mes_line = (
        f"• SLA: {e_mes} *{k_mes['sla']:.1f}%* | ATR acumulado: {k_mes['atrasados']}"
        if k_mes else "• _Dados do mes indisponiveis_"
    )

    e_on         = emoji_circle(on_sla)
    nao_ent_line = f"• ❌ {on_nao_ent} nao entregues -- viraram ATR\n" if on_nao_ent > 0 else ""

    msg = (
        f"\U0001f305 *RESUMO AGV -- {data_fmt} | {hora}*\n"
        f"━━━━━━━━━━━━━\n"
        f"\U0001f4cb *Vencimentos de ontem ({ontem_fmt})*\n"
        f"• {on_total} pedidos com prazo no dia\n"
        f"• {e_on} {on_ok} entregues no prazo ({on_sla:.0f}%) | {on_atr_ent} ATR\n"
        f"{nao_ent_line}"
        f"\n"
        f"\U0001f6a8 *Urgente hoje ({hoje_fmt_r}) -- {hj_pend} pendentes*\n"
        f"• {hj_total} pedidos vencem hoje | {hj_ok + hj_atr} ja entregues\n"
        f"• \U0001f69a {hj_transito} em transito\n"
        f"• \U0001f4ec {hj_expedindo} expedindo (risco alto)\n"
        f"• \U0001f504 {hj_insucesso} insucesso (retentativa urgente)\n"
        f"\n"
        f"\U0001f4c5 *Mes acumulado{av}*\n"
        f"{mes_line}\n"
        f"━━━━━━━━━━━━━"
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

    # fetch_chunked garante que 10 dias nao va dar timeout (4 chamadas de 3 dias)
    df10 = fetch_chunked(inicio_10d, hoje_s, chunk_days=3)
    if df10.empty:
        return {"mensagem": f"_Painel indisponivel ({hoje_fmt} | {hora}) -- API sem resposta_"}

    # tenta mes completo; se timeout, usa df10 como fallback
    df_mes   = fetch(mes_s, hoje_s)
    fallback = df_mes.empty
    if fallback:
        df_mes = df10
    av = ' _(ultimos 10d)_' if fallback else ''

    sem1h_mes = df_mes[~df_mes['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h_mes['StatusOp']  = _apply_status_map(sem1h_mes['Status'])
    sem1h_mes['DataSLA_d'] = pd.to_datetime(sem1h_mes['DataSLA_Sistema'], errors='coerce').dt.date

    # abertos = todos que nao foram entregues/cancelados/devolucao
    abertos_mes  = sem1h_mes[~sem1h_mes['StatusOp'].isin(['Entregue','Cancelado','Devolucao'])].copy()
    vencidos_mes = abertos_mes[abertos_mes['DataSLA_d'].apply(lambda x: pd.notna(x) and x < hoje)]
    n_vencidos   = len(vencidos_mes)

    # status real: conta os abertos (nao apenas os de hoje)
    c           = abertos_mes['StatusOp'].value_counts().to_dict()
    n_entregues = int((sem1h_mes['StatusOp'] == 'Entregue').sum())

    linhas_cont = []
    if not vencidos_mes.empty:
        col = 'LojaGrupo' if 'LojaGrupo' in vencidos_mes.columns else 'LojaNome'
        for nome, cnt in vencidos_mes[col].value_counts().items():
            if not nome or str(nome).strip() == '': continue
            nome_c = str(nome).replace('GRUPO ','').replace(' DROGASIL','').title()[:30]
            linhas_cont.append(f"  \U0001f534 {nome_c}: {cnt} vencidos")
    cont_fmt = '\n'.join(linhas_cont) if linhas_cont else '  Nenhum vencido'

    linhas_polo = []
    if not vencidos_mes.empty and 'Polo' in vencidos_mes.columns:
        for polo, cnt in vencidos_mes[vencidos_mes['Polo'].str.strip().ne('')]['Polo'].value_counts().items():
            linhas_polo.append(f"  \U0001f534 {polo}: {cnt} vencidos")
    polo_fmt = '\n'.join(linhas_polo) if linhas_polo else '  Nenhum vencido'

    msg = (
        f"⚙️ *PAINEL -- {hoje_fmt} | {hora}*\n"
        f"━━━━━━━━━━━━\n"
        f"\U0001f4cb *Status em aberto{av}*\n"
        f"• ✅ Entregues no periodo: {n_entregues}\n"
        f"• \U0001f69a Em Transito: {c.get('Em Transito', 0)}\n"
        f"• \U0001f4ec Expedindo: {c.get('Expedindo', 0)}\n"
        f"• \U0001f504 Insucesso: {c.get('Insucesso', 0)}\n"
        f"• ↩️ Devolucao: {c.get('Devolucao', 0)}\n"
        f"• ⏳ Vencidos (SLA vencido): {n_vencidos}\n"
        f"\n"
        f"\U0001f4cb *Por Contratante (vencidos{av})*\n"
        f"{cont_fmt}\n"
        f"\n"
        f"\U0001f3e2 *Por Polo (vencidos{av})*\n"
        f"{polo_fmt}\n"
        f"━━━━━━━━━━━━"
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
        return {"mensagem": f"_Fechamento indisponivel ({data_fmt} | {hora}) -- API sem resposta_"}
    k_hj  = kpis(df_hj)
    k_mes = kpis(df_mes) if not df_mes.empty else k_hj
    av    = ' _(parcial)_' if df_mes.empty else ''
    st = status_operacional(df_mes) if not df_mes.empty else {}
    c  = st.get('contagens', {})
    lojas_alert = fmt_lojas_criticas(st.get('lojas_criticas', []))
    grupos_fmt  = fmt_status_grupos(st.get('grupos', {}))
    polos_fmt   = fmt_polo(k_mes['polos'], top=7)
    e_hj  = emoji_circle(k_hj['sla'])
    e_mes = emoji_circle(k_mes['sla'])
    lojas_section = ('\n' + lojas_alert) if lojas_alert else ''
    msg = (
        f"\U0001f306 *FECHAMENTO AGV -- {data_fmt} | {hora}*\n"
        f"━━━━━━━━━━━\n"
        f"\U0001f4e6 *Dia de hoje*\n"
        f"• Integrados: {k_hj['total']} | D+: {k_hj['sem1h']}\n"
        f"• Entregues: {k_hj['entregues']} | Pendentes: {k_hj['pendentes']}\n"
        f"• ATR: {k_hj['atrasados']}\n"
        f"• SLA do dia: {e_hj} *{k_hj['sla']:.1f}%*\n"
        f"\n"
        f"\U0001f4c5 *Acumulado do mes{av}*\n"
        f"• Total D+: {k_mes['sem1h']}\n"
        f"• Entregues: {k_mes['entregues']}\n"
        f"• SLA mes: {e_mes} *{k_mes['sla']:.1f}%*\n"
        f"• ATR: {k_mes['atrasados']}\n"
        f"\n"
        f"\U0001f4cb *Status operacional (mes{av})*\n"
        f"• ✅ Entregues: {c.get('Entregue', 0)}\n"
        f"• \U0001f69a Em Transito: {c.get('Em Transito', 0)}\n"
        f"• \U0001f4ec Expedindo: {c.get('Expedindo', 0)}\n"
        f"• \U0001f504 Insucesso: {c.get('Insucesso', 0)}\n"
        f"• ↩️ Devolucao: {c.get('Devolucao', 0)}\n"
        f"• ❌ Cancelado: {c.get('Cancelado', 0)}\n"
        f"• ⏳ Vencidos: {k_mes['pendentes']}\n"
        f"\n"
        f"\U0001f3e2 *Polos SLA mes{av}*\n"
        f"{polos_fmt}\n"
        f"\n"
        f"\U0001f4ca *Por Contratante (mes{av})*\n"
        f"{grupos_fmt}"
        f"{lojas_section}\n"
        f"━━━━━━━━━━━━━━"
    )
    return {"mensagem": msg.strip()}

@app.get("/cliente")
def cliente(nome: str = "raia"):
    """Raio-x de um contratante. Ex: /cliente?nome=raia"""
    hoje  = date.today()
    mes_s = hoje.replace(day=1).strftime('%Y-%m-%d')
    hoje_s = hoje.strftime('%Y-%m-%d')
    ts     = now_brt()
    data_fmt = ts.strftime('%d/%m/%Y')
    hora     = ts.strftime('%H:%M')

    df_mes = fetch_chunked(mes_s, hoje_s, chunk_days=3)
    if df_mes.empty:
        return {"mensagem": f"_Raio-x indisponivel ({data_fmt} | {hora}) -- API sem resposta_"}

    sem1h = df_mes[~df_mes['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h['StatusOp']  = _apply_status_map(sem1h['Status'])
    sem1h['DataSLA_d'] = pd.to_datetime(sem1h['DataSLA_Sistema'], errors='coerce').dt.date

    # filtra pelo nome do contratante (LojaGrupo ou LojaNome)
    nome_norm = sem_acento(nome)
    col_busca = 'LojaGrupo' if 'LojaGrupo' in sem1h.columns else 'LojaNome'
    mask = sem1h[col_busca].apply(lambda x: nome_norm in sem_acento(str(x)))
    df_c = sem1h[mask].copy()

    if df_c.empty:
        grupos_disp = sem1h[col_busca].value_counts().head(10).index.tolist()
        return {"mensagem": f"_Contratante '{nome}' nao encontrado. Disponiveis: {grupos_disp}_"}

    nome_exib = df_c[col_busca].mode()[0] if not df_c[col_busca].empty else nome.title()
    total     = len(df_c)
    entregues = int((df_c['Status_SLA'] != 'Pendente').sum())
    no_prazo  = int((df_c['Status_SLA'] == 'No Prazo').sum())
    atrasados = int((df_c['Status_SLA'] == 'Atrasado').sum())
    pendentes = int((df_c['Status_SLA'] == 'Pendente').sum())
    sla       = no_prazo / entregues * 100 if entregues else 0

    # vencidos: abertos com SLA ja vencido
    abertos   = df_c[~df_c['StatusOp'].isin(['Entregue','Cancelado','Devolucao'])]
    vencidos  = abertos[abertos['DataSLA_d'].apply(lambda x: pd.notna(x) and x < hoje)]
    n_venc    = len(vencidos)

    # breakdown dos vencidos por StatusOp
    vc = vencidos['StatusOp'].value_counts().to_dict() if n_venc else {}

    # por polo
    polo_lines = []
    if 'Polo' in df_c.columns:
        for polo, g in df_c[df_c['Polo'].str.strip().ne('')].groupby('Polo'):
            t  = len(g)
            ent_g = g[g['Status_SLA'] != 'Pendente']
            ok_g  = int((ent_g['Status_SLA'] == 'No Prazo').sum())
            ne_g  = len(ent_g)
            sla_g = ok_g / ne_g * 100 if ne_g else 0
            venc_g = len(g[~g['StatusOp'].isin(['Entregue','Cancelado','Devolucao']) &
                           g['DataSLA_d'].apply(lambda x: pd.notna(x) and x < hoje)])
            e = emoji_circle(sla_g)
            polo_lines.append((t, f"{e} *{polo}*: {sla_g:.0f}% | {t} ped = {ok_g} Ok / {ne_g-ok_g} ATR / {t-ne_g} PEN | {venc_g} vencidos"))
    polo_lines.sort(key=lambda x: -x[0])
    polo_fmt = '\n'.join(l for _, l in polo_lines) if polo_lines else '_Sem dados de polo_'

    e_sla = emoji_circle(sla)
    venc_detail = (
        f"  \U0001f69a Em Transito: {vc.get('Em Transito',0)}\n"
        f"  \U0001f4ec Expedindo: {vc.get('Expedindo',0)}\n"
        f"  \U0001f504 Insucesso: {vc.get('Insucesso',0)}"
    ) if n_venc else "  Nenhum vencido"

    nome_titulo = str(nome_exib).replace('GRUPO ','').replace(' DROGASIL','').title()
    msg = (
        f"\U0001f50d *RAIO-X: {nome_titulo} -- {data_fmt} | {hora}*\n"
        f"━━━━━━━━━━━━━\n"
        f"\U0001f4e6 *Volume do mes*\n"
        f"• Total D+: {total}\n"
        f"• Entregues: {entregues} | Pendentes: {pendentes}\n"
        f"• No Prazo: {no_prazo} | ATR: {atrasados}\n"
        f"• SLA: {e_sla} *{sla:.1f}%*\n"
        f"\n"
        f"⏳ *Vencidos em aberto: {n_venc}*\n"
        f"{venc_detail}\n"
        f"\n"
        f"\U0001f3e2 *Por Polo*\n"
        f"{polo_fmt}\n"
        f"━━━━━━━━━━━━━"
    )
    return {"mensagem": msg.strip()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

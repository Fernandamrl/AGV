"""
AGV Logística — API de Reports de SLA
FastAPI que o n8n chama para gerar os reports do WhatsApp.

USO:
    pip install fastapi uvicorn requests pandas holidays
    python agv_sla_api.py

ENDPOINTS:
    GET /resumo      → Report das 07h (fechamento do dia anterior)
    GET /painel      → Report das 13h (situação em tempo real)
    GET /fechamento  → Report das 18h (consolidado do dia)
    GET /health      → Verifica se a API está rodando
"""

from fastapi import FastAPI
from datetime import datetime, timedelta, date
import unicodedata, ast, requests, pandas as pd, holidays

app = FastAPI(title="AGV SLA API")

# ─── CONFIGURAÇÃO ─────────────────────────────────────────────────

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


# ─── UTILITÁRIOS ──────────────────────────────────────────────────

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


# ─── FETCH + PROCESSAMENTO ────────────────────────────────────────

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

    # Desempacota campos aninhados
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

    # Rename campos
    rename = {'Id':'ID','DataSla':'DataSLA_Sistema','SLANoPrazo':'SLANoPrazo_Sistema',
              'Polo':'Polo'}
    df = df.rename(columns={k:v for k,v in rename.items() if k in df.columns})

    for col in ['CidadeDestino','CEP','LojaNome','LojaGrupo','Polo','DataConclusao','DataSLA_Sistema']:
        if col not in df.columns: df[col] = ''

    df['CidadeDestino'] = df['CidadeDestino'].str.strip().str.title()

    # Calcula SLA
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
            dias,tipo = 1,('D+1 (Grande SP)' if CEP_SP_MIN<=cep_n<=CEP_SP_MAX else 'D+1 (padrão)')

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

    # Diff SLA
    def diff(row):
        calc = row.get('SLA_calc')
        sist = row.get('DataSLA_Sistema')
        if calc is None or not sist or pd.isna(sist): return None
        try:
            d = pd.to_datetime(sist).date()
            return (d - calc).days
        except: return None

    df['Diff_SLA_dias'] = df.apply(diff, axis=1)

    return df


def kpis(df: pd.DataFrame) -> dict:
    sem1h = df[~df['Tipo_SLA'].str.contains('1Hr', na=False)]
    ent   = sem1h[sem1h['Status_SLA'] != 'Pendente']
    n = len(ent); ok = (ent['Status_SLA']=='No Prazo').sum(); at = n - ok
    pend = (sem1h['Status_SLA']=='Pendente').sum()
    sla  = ok/n*100 if n else 0

    # Por polo
    polos = {}
    if 'Polo' in sem1h.columns:
        for polo, g in sem1h[sem1h['Polo'].str.strip().ne('')].groupby('Polo'):
            ge = g[g['Status_SLA']!='Pendente']
            ne = len(ge); oe = (ge['Status_SLA']=='No Prazo').sum()
            if ne == 0: continue
            polos[polo] = {'total':len(g),'entregues':ne,'ok':oe,'sla':oe/ne*100}

    # Cidades críticas (atrasadas)
    cidades_crit = {}
    if 'CidadeDestino' in sem1h.columns:
        for cid, g in sem1h[sem1h['CidadeDestino'].str.strip().ne('')].groupby('CidadeDestino'):
            ge = g[g['Status_SLA']!='Pendente']
            ne = len(ge); oe = (ge['Status_SLA']=='No Prazo').sum()
            if ne >= 5 and (oe/ne*100 < 85):
                cidades_crit[cid] = {'total':ne,'ok':oe,'sla':oe/ne*100}

    return {
        'total': len(df), 'sem1h': len(sem1h), 'entregues': n,
        'ok': ok, 'atrasados': at, 'pendentes': pend, 'sla': sla,
        'polos': polos, 'cidades_criticas': cidades_crit
    }



STATUS_MAP = {
    'Entrega Realizada':'Entregue','Acareação: Entrega Realizada':'Entregue',
    'Chegada no Destinatário':'Entregue','Coletado na Loja':'Entregue',
    'Despachado':'Em Trânsito','Chegada na Base':'Em Trânsito',
    'Transferência entre unidades':'Em Trânsito','Rota de Entrega':'Em Trânsito',
    'Integração Recebida':'Expedindo','Não Coletado':'Expedindo',
    'Coleta Cancelado pelo Remetente':'Cancelado',
    'Entrega Cancelada pelo Remetente':'Cancelado','Cancelado pelo Destinatário':'Cancelado',
    'Cliente Ausente':'Insucesso','Localidade Não Atendida':'Insucesso',
    'Endereço Não Localizado':'Insucesso','Número não Localizado':'Insucesso',
    'Estabelecimento Fechado':'Insucesso','Destinatário Não Encontrado':'Insucesso',
    'Recusado por Terceiro':'Insucesso','Outras Ocorrências':'Insucesso',
    'Em Devolução':'Devolução','Devolvido':'Devolução',
    'Pedido Extraviado':'Problema','Pedido Danificado':'Problema','Aguardando Tratativa':'Problema',
}


def status_operacional(df: pd.DataFrame) -> dict:
    sem1h = df[~df['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h['StatusOp'] = sem1h['Status'].map(STATUS_MAP).fillna('Outros')
    c = sem1h['StatusOp'].value_counts().to_dict()
    lojas_criticas = []
    if 'LojaNome' in sem1h.columns:
        for loja, g in sem1h[sem1h['LojaNome'].str.strip().ne('')].groupby('LojaNome'):
            t = len(g); ins = (g['StatusOp']=='Insucesso').sum()
            if t >= 20 and ins/t > 0.10:
                lojas_criticas.append({'loja':loja,'total':t,'insucesso':ins,'pct':ins/t*100})
    lojas_criticas.sort(key=lambda x: -x['pct'])
    grupos = {}
    if 'LojaGrupo' in sem1h.columns:
        for grp, g in sem1h[sem1h['LojaGrupo'].str.strip().ne('')].groupby('LojaGrupo'):
            t = len(g); ent = (g['StatusOp']=='Entregue').sum(); ins = (g['StatusOp']=='Insucesso').sum()
            grupos[grp] = {'total':t,'entregue':ent,'insucesso':ins,'pct_ent':ent/t*100,'pct_ins':ins/t*100}
    return {'contagens':c,'lojas_criticas':lojas_criticas,'grupos':grupos}

# ─── FORMATAÇÃO DAS MENSAGENS ────────────────────────────────────

def fmt_status_grupos(grupos):
    linhas = []
    for grp, d in sorted(grupos.items(), key=lambda x: -x[1]['total']):
        nome = grp.replace('GRUPO ','').replace(' DROGASIL','').title()[:22]
        e = emoji_sla(d['pct_ent'])
        ins_str = f" | 🔄 {d['pct_ins']:.0f}% ins" if d['pct_ins'] >= 5 else ''
        linhas.append(f"{e} *{nome}*: {d['pct_ent']:.0f}% ({d['total']} ped){ins_str}")
    return '\n'.join(linhas) if linhas else '_Sem dados_'


def fmt_lojas_criticas(lojas):
    if not lojas: return ''
    linhas = ['⚠️ *Lojas com insucesso >10%:*']
    for l in lojas[:5]:
        nome = l['loja'][:28]; pct = l['pct']; ins = l['insucesso']; tot = l['total']
        linhas.append(f"  🔴 {nome}: {pct:.0f}% ({ins}/{tot})")
    return '\n'.join(linhas)


def fmt_polo_lojas(df_grp, max_lojas=2):
    """Linha por polo com top lojas inline: HUB LSX: 12 → Raia CD GRU (8), Afterclick (4)"""
    if df_grp.empty: return '  ✅ Nenhum'
    linhas = []
    polo_col = 'Polo' if 'Polo' in df_grp.columns else None
    loja_col = 'LojaNome' if 'LojaNome' in df_grp.columns else None
    if polo_col:
        for polo, gp in sorted(df_grp.groupby(polo_col), key=lambda x: -len(x[1])):
            if not polo or str(polo).strip() == '': continue
            n = len(gp)
            if loja_col:
                top = gp[loja_col].value_counts().head(max_lojas)
                lojas_str = ', '.join(f"{l[:20]} ({c})" for l,c in top.items())
                linhas.append(f"  {polo}: {n} → {lojas_str}")
            else:
                linhas.append(f"  {polo}: {n}")
    return '\n'.join(linhas) if linhas else '  ✅ Nenhum'


def fmt_polo_resumo(df_grp):
    """Só totais por polo em uma linha: HUB LSX: 28 | AGV SANTANA: 12"""
    if df_grp.empty: return '  ✅ Nenhum'
    polo_col = 'Polo' if 'Polo' in df_grp.columns else None
    if not polo_col: return f"  {len(df_grp)} pedidos"
    partes = []
    for polo, gp in sorted(df_grp.groupby(polo_col), key=lambda x: -len(x[1])):
        if polo and str(polo).strip(): partes.append(f"{polo}: {len(gp)}")
    return '  ' + ' | '.join(partes) if partes else '  ✅ Nenhum'


def emoji_sla(pct):
    if pct >= 95: return '🟢'
    if pct >= 88: return '🟡'
    return '🔴'

def fmt_polo(polos: dict, top=5) -> str:
    if not polos: return '_Sem dados de polo_'
    ordenados = sorted(polos.items(), key=lambda x: x[1]['entregues'], reverse=True)[:top]
    linhas = []
    for nome, d in ordenados:
        e = emoji_sla(d['sla'])
        linhas.append(f"{e} *{nome}*: {d['sla']:.0f}% | {d['ok']} entregues / {d['total']} ped")
    return '\n'.join(linhas)

def fmt_alertas(cidades: dict) -> str:
    if not cidades: return '✅ Nenhuma cidade crítica identificada'
    ordenados = sorted(cidades.items(), key=lambda x: x[1]['sla'])
    linhas = ['⚠️ *Cidades abaixo de 85% SLA (mín. 5 entregas):*']
    for nome, d in ordenados:
        linhas.append(f"  🔴 {nome}: {d['sla']:.0f}% ({d['ok']}/{d['total']})")
    return '\n'.join(linhas)


# ─── ENDPOINTS ────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "hora": datetime.now().strftime("%d/%m/%Y %H:%M")}


@app.get("/resumo")
def resumo():
    """07h — Fechamento do dia anterior."""
    hoje  = date.today()
    ontem = (hoje - timedelta(days=1)).strftime('%Y-%m-%d')
    mes   = hoje.replace(day=1).strftime('%Y-%m-%d')
    hoje_s = hoje.strftime('%Y-%m-%d')

    df_on = fetch(ontem, ontem)
    df_mes = fetch(mes, hoje_s)

    if df_on.empty:
        return {"mensagem": f"⚠️ Sem dados para {ontem}"}

    k_on  = kpis(df_on)
    k_mes = kpis(df_mes) if not df_mes.empty else k_on

    msg = f"""🌅 *RESUMO AGV — {datetime.now().strftime('%d/%m/%Y')} | 07h*
━━━━━━━━━━━━━━━━━━━━━
📦 *Ontem ({ontem})*
• Pedidos D+: {k_on['sem1h']}
• Entregues: {k_on['entregues']}
• No Prazo: {k_on['ok']} | Atrasados: {k_on['atrasados']}
• SLA: {emoji_sla(k_on['sla'])} *{k_on['sla']:.1f}%*

📅 *Acumulado do mês*
• Total D+: {k_mes['sem1h']}
• SLA mês: {emoji_sla(k_mes['sla'])} *{k_mes['sla']:.1f}%*
• Atrasados mês: {k_mes['atrasados']}

🏢 *SLA por Polo (ontem)*
{fmt_polo(k_on['polos'])}

{fmt_alertas(k_on['cidades_criticas'])}
━━━━━━━━━━━━━━━━━━━━━"""

    return {"mensagem": msg.strip()}


@app.get("/painel")
def painel():
    """Operacional — a cada 3h (08,11,14,17,20). Eixo: DataSLA_Sistema."""
    hoje   = date.today()
    amanha = hoje + timedelta(days=1)
    mes_s  = hoje.replace(day=1).strftime('%Y-%m-%d')
    hoje_s = hoje.strftime('%Y-%m-%d')
    hora   = datetime.now().strftime('%H:%M')

    df_mes  = fetch(mes_s, hoje_s)
    df_hoje = fetch(hoje_s, hoje_s)
    if df_mes.empty:
        return {"mensagem": f"⚠️ Sem dados ({hoje_s})"}

    sem1h = df_mes[~df_mes['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h['StatusOp'] = sem1h['Status'].map(STATUS_MAP).fillna('Outros')
    sem1h['DataSLA_d'] = pd.to_datetime(sem1h['DataSLA_Sistema'], errors='coerce').dt.date

    # Status operacional — pedidos de hoje
    hoje_sem1h = df_hoje[~df_hoje['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    hoje_sem1h['StatusOp'] = hoje_sem1h['Status'].map(STATUS_MAP).fillna('Outros')
    c = hoje_sem1h['StatusOp'].value_counts().to_dict()

    # Pedidos em aberto (não entregues, não cancelados) — mês inteiro
    abertos = sem1h[~sem1h['StatusOp'].isin(['Entregue','Cancelado','Devolução'])].copy()

    atrasados  = abertos[abertos['DataSLA_d'].apply(lambda x: x is not None and x < hoje)]
    vence_hoje = abertos[abertos['DataSLA_d'] == hoje]
    vence_aman = abertos[abertos['DataSLA_d'] == amanha]

    msg = f"""⚙️ *PAINEL — {hoje.strftime('%d/%m/%Y')} | {hora}*
━━━━━━━━━━━━━━━━━━━━━
📋 *Status operacional (dia)*
• ✅ Entregues: {c.get('Entregue', 0)}
• 🚚 Em Trânsito: {c.get('Em Trânsito', 0)}
• 📬 Expedindo: {c.get('Expedindo', 0)}
• 🔄 Insucesso: {c.get('Insucesso', 0)}
• ↩️ Devolução: {c.get('Devolução', 0)}
• ❌ Cancelado: {c.get('Cancelado', 0)}

🔴 *ATRASADOS: {len(atrasados)} pedidos*
{fmt_polo_lojas(atrasados)}

🚨 *VENCEM HOJE: {len(vence_hoje)} pedidos*
{fmt_polo_lojas(vence_hoje)}

⏳ *VENCEM AMANHÃ: {len(vence_aman)} pedidos*
{fmt_polo_resumo(vence_aman)}
━━━━━━━━━━━━━━━━━━━━━"""

    return {"mensagem": msg.strip()}


@app.get("/fechamento")
def fechamento():
    """18h — Consolidado do dia."""
    hoje   = date.today()
    hoje_s = hoje.strftime('%Y-%m-%d')
    mes_s  = hoje.replace(day=1).strftime('%Y-%m-%d')

    df_hj  = fetch(hoje_s, hoje_s)
    df_mes = fetch(mes_s, hoje_s)
    if df_hj.empty:
        return {"mensagem": f"⚠️ Sem dados para fechamento de {hoje_s}"}

    k_hj  = kpis(df_hj)
    k_mes = kpis(df_mes) if not df_mes.empty else k_hj

    sem1h = df_mes[~df_mes['Tipo_SLA'].str.contains('1Hr', na=False)]
    div_count = sem1h['Diff_SLA_dias'].notna().sum()
    div_neg   = (sem1h['Diff_SLA_dias'].fillna(0) < 0).sum()
    div_pos   = (sem1h['Diff_SLA_dias'].fillna(0) > 0).sum()

    st = status_operacional(df_mes) if not df_mes.empty else {}
    c  = st.get('contagens', {})
    lojas_alert = fmt_lojas_criticas(st.get('lojas_criticas', []))

    msg = f"""🌆 *FECHAMENTO AGV — {datetime.now().strftime('%d/%m/%Y')} | 18h*
━━━━━━━━━━━━━━━━━━━━━
📦 *Dia de hoje*
• Integrados: {k_hj['total']} | D+: {k_hj['sem1h']}
• Entregues: {k_hj['entregues']} | Pendentes: {k_hj['pendentes']}
• Atrasados: {k_hj['atrasados']}
• SLA do dia: {emoji_sla(k_hj['sla'])} *{k_hj['sla']:.1f}%*

📅 *Acumulado do mês*
• Total D+: {k_mes['sem1h']}
• Entregues: {k_mes['entregues']}
• SLA mês: {emoji_sla(k_mes['sla'])} *{k_mes['sla']:.1f}%*
• Atrasados: {k_mes['atrasados']}

🏢 *Ranking de Polos (mês)*
{fmt_polo(k_mes['polos'], top=7)}

📋 *Status operacional (mês)*
• ✅ Entregues: {c.get('Entregue', 0)}
• 🚚 Em Trânsito: {c.get('Em Trânsito', 0)}
• 📬 Expedindo: {c.get('Expedindo', 0)}
• 🔄 Insucesso: {c.get('Insucesso', 0)}
• ↩️ Devolução: {c.get('Devolução', 0)}
• ❌ Cancelado: {c.get('Cancelado', 0)}

📊 *Por Contratante (mês)*
{fmt_status_grupos(st.get('grupos', {}))}
{"" if not lojas_alert else chr(10) + lojas_alert}
🔎 *Divergências SLA (nosso vs sistema)*
• Pedidos com DataSLA diferente: {div_count}
• Sistema mais apertado: {div_neg} | mais folgado: {div_pos}

{fmt_alertas(k_mes['cidades_criticas'])}
━━━━━━━━━━━━━━━━━━━━━"""

    return {"mensagem": msg.strip()}


# ─── START ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

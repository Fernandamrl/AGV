"""
AGV Logística — API de Reports de SLA  (v8)
FastAPI que o n8n chama para gerar os reports do WhatsApp.

ENDPOINTS:
    GET /resumo      → Report das 07h (fechamento do dia anterior)
    GET /painel      → Operacional a cada 3h (08,11,14,17,20)
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

    polos = {}
    if 'Polo' in sem1h.columns:
        for polo, g in sem1h[sem1h['Polo'].str.strip().ne('')].groupby('Polo'):
            ge = g[g['Status_SLA']!='Pendente']
            ne = len(ge); oe = (ge['Status_SLA']=='No Prazo').sum()
            if ne == 0: continue
            polos[polo] = {'total':len(g),'entregues':ne,'ok':oe,'sla':oe/ne*100}

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
    'Entrega Realizada':'Entregue','Acareacao: Entrega Realizada':'Entregue',
    'Chegada no Destinatario':'Entregue','Coletado na Loja':'Entregue',
    'Despachado':'Em Transito','Chegada na Base':'Em Transito',
    'Transferencia entre unidades':'Em Transito','Rota de Entrega':'Em Transito',
    'Integracao Recebida':'Expedindo','Nao Coletado':'Expedindo',
    'Coleta Cancelado pelo Remetente':'Cancelado',
    'Entrega Cancelada pelo Remetente':'Cancelado','Cancelado pelo Destinatario':'Cancelado',
    'Cliente Ausente':'Insucesso','Localidade Nao Atendida':'Insucesso',
    'Endereco Nao Localizado':'Insucesso','Numero nao Localizado':'Insucesso',
    'Estabelecimento Fechado':'Insucesso','Destinatario Nao Encontrado':'Insucesso',
    'Recusado por Terceiro':'Insucesso','Outras Ocorrencias':'Insucesso',
    'Em Devolucao':'Devolucao','Devolvido':'Devolucao',
    'Pedido Extraviado':'Problema','Pedido Danificado':'Problema','Aguardando Tratativa':'Problema',
    # Com acentos (API pode retornar ambos)
    'Entrega Realizada':'Entregue','Acareação: Entrega Realizada':'Entregue',
    'Chegada no Destinatário':'Entregue',
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


# ─── FORMATAÇÃO ────────────────────────────────────────────────────

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
    if not cidades: return 'Nenhuma cidade critica identificada'
    ordenados = sorted(cidades.items(), key=lambda x: x[1]['sla'])
    linhas = ['*Cidades abaixo de 85% SLA (min. 5 entregas):*']
    for nome, d in ordenados:
        linhas.append(f"  {nome}: {d['sla']:.0f}% ({d['ok']}/{d['total']})")
    return '\n'.join(linhas)

def fmt_status_grupos(grupos):
    linhas = []
    for grp, d in sorted(grupos.items(), key=lambda x: -x[1]['total']):
        nome = grp.replace('GRUPO ','').replace(' DROGASIL','').title()[:22]
        e = emoji_sla(d['pct_ent'])
        ins_str = f" | {d['pct_ins']:.0f}% ins" if d['pct_ins'] >= 5 else ''
        linhas.append(f"{e} *{nome}*: {d['pct_ent']:.0f}% ({d['total']} ped){ins_str}")
    return '\n'.join(linhas) if linhas else '_Sem dados_'

def fmt_lojas_criticas(lojas):
    if not lojas: return ''
    linhas = ['*Lojas com insucesso >10%:*']
    for l in lojas[:5]:
        nome = l['loja'][:28]; pct = l['pct']; ins = l['insucesso']; tot = l['total']
        linhas.append(f"  {nome}: {pct:.0f}% ({ins}/{tot})")
    return '\n'.join(linhas)


# ─── ENDPOINTS ────────────────────────────────────────────────────

@app.get("/health")
def health():
    hora = pd.Timestamp.now('America/Sao_Paulo').strftime('%d/%m/%Y %H:%M')
    return {"status": "ok", "hora": hora}


@app.get("/resumo")
def resumo():
    hoje  = date.today()
    ontem = (hoje - timedelta(days=1)).strftime('%Y-%m-%d')
    mes   = hoje.replace(day=1).strftime('%Y-%m-%d')
    hoje_s = hoje.strftime('%Y-%m-%d')
    data_fmt = pd.Timestamp.now('America/Sao_Paulo').strftime('%d/%m/%Y')

    df_on  = fetch(ontem, ontem)
    df_mes = fetch(mes, hoje_s)

    if df_on.empty:
        return {"mensagem": f"Sem dados para {ontem}"}

    k_on  = kpis(df_on)
    k_mes = kpis(df_mes) if not df_mes.empty else k_on

    polos_fmt  = fmt_polo(k_on['polos'])
    alerta_fmt = fmt_alertas(k_on['cidades_criticas'])
    sla_on_e   = emoji_sla(k_on['sla'])
    sla_mes_e  = emoji_sla(k_mes['sla'])

    msg = (
        f"🌅 *RESUMO AGV — {data_fmt} | 07h*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *Ontem ({ontem})*\n"
        f"• Pedidos D+: {k_on['sem1h']}\n"
        f"• Entregues: {k_on['entregues']}\n"
        f"• No Prazo: {k_on['ok']} | Atrasados: {k_on['atrasados']}\n"
        f"• SLA: {sla_on_e} *{k_on['sla']:.1f}%*\n"
        f"\n"
        f"📅 *Acumulado do mes*\n"
        f"• Total D+: {k_mes['sem1h']}\n"
        f"• SLA mes: {sla_mes_e} *{k_mes['sla']:.1f}%*\n"
        f"• Atrasados mes: {k_mes['atrasados']}\n"
        f"\n"
        f"🏢 *SLA por Polo (ontem)*\n"
        f"{polos_fmt}\n"
        f"\n"
        f"{alerta_fmt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    return {"mensagem": msg.strip()}


@app.get("/painel")
def painel():
    """Operacional a cada 3h (08,11,14,17,20).
    Chamada 1: 10 dias — status operacional do dia.
    Chamada 2: mes todo — atrasados por contratante e polo."""
    hoje       = date.today()
    inicio_10d = (hoje - timedelta(days=10)).strftime('%Y-%m-%d')
    mes_s      = hoje.replace(day=1).strftime('%Y-%m-%d')
    hoje_s     = hoje.strftime('%Y-%m-%d')
    hoje_fmt   = hoje.strftime('%d/%m/%Y')
    hora       = pd.Timestamp.now('America/Sao_Paulo').strftime('%H:%M')

    # Chamada 1: 10 dias — status operacional do dia
    df10 = fetch(inicio_10d, hoje_s)
    if df10.empty:
        return {"mensagem": f"Sem dados ({hoje_s})"}

    sem1h_10 = df10[~df10['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h_10['StatusOp'] = sem1h_10['Status'].map(STATUS_MAP).fillna('Outros')
    sem1h_10['DataInteg_d'] = (
        pd.to_datetime(sem1h_10['DataIntegracao'], utc=True, errors='coerce')
        .dt.tz_convert('America/Sao_Paulo').dt.date
    )
    hoje_df = sem1h_10[sem1h_10['DataInteg_d'] == hoje]
    c = hoje_df['StatusOp'].value_counts().to_dict()

    # Chamada 2: mes todo — atrasados
    df_mes = pd.DataFrame()
    try:
        df_mes = fetch(mes_s, hoje_s)
    except Exception:
        try:
            inicio_20d = (hoje - timedelta(days=20)).strftime('%Y-%m-%d')
            df_mes = fetch(inicio_20d, hoje_s)
        except Exception:
            df_mes = df10

    sem1h_mes = df_mes[~df_mes['Tipo_SLA'].str.contains('1Hr', na=False)].copy()
    sem1h_mes['StatusOp']  = sem1h_mes['Status'].map(STATUS_MAP).fillna('Outros')
    sem1h_mes['DataSLA_d'] = pd.to_datetime(sem1h_mes['DataSLA_Sistema'], errors='coerce').dt.date

    abertos_mes   = sem1h_mes[~sem1h_mes['StatusOp'].isin(['Entregue','Cancelado','Devolucao','Devolução'])].copy()
    atrasados_mes = abertos_mes[abertos_mes['DataSLA_d'].apply(lambda x: pd.notna(x) and x < hoje)]
    n_atrasados   = len(atrasados_mes)

    # Por contratante
    linhas_cont = []
    if not atrasados_mes.empty:
        col = 'LojaGrupo' if 'LojaGrupo' in atrasados_mes.columns else 'LojaNome'
        for nome, cnt in atrasados_mes[col].value_counts().items():
            if not nome or str(nome).strip() == '': continue
            nome_c = str(nome).replace('GRUPO ','').replace(' DROGASIL','').title()[:30]
            linhas_cont.append(f"  🔴 {nome_c}: {cnt} atrasados")
    cont_fmt = '\n'.join(linhas_cont) if linhas_cont else '  Nenhum atrasado'

    # Por polo
    linhas_polo = []
    if not atrasados_mes.empty and 'Polo' in atrasados_mes.columns:
        for polo, cnt in atrasados_mes[atrasados_mes['Polo'].str.strip().ne('')]['Polo'].value_counts().items():
            linhas_polo.append(f"  🔴 {polo}: {cnt} atrasados")
    polo_fmt = '\n'.join(linhas_polo) if linhas_polo else '  Nenhum atrasado'

    ent   = c.get('Entregue', 0)
    tran  = c.get('Em Trânsito', c.get('Em Transito', 0))
    exp   = c.get('Expedindo', 0)
    ins   = c.get('Insucesso', 0)
    dev   = c.get('Devolução', c.get('Devolucao', 0))
    canc  = c.get('Cancelado', 0)

    msg = (
        f"⚙️ *PAINEL — {hoje_fmt} | {hora}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Status operacional (dia)*\n"
        f"• ✅ Entregues: {ent}\n"
        f"• 🚚 Em Trânsito: {tran}\n"
        f"• 📬 Expedindo: {exp}\n"
        f"• 🔄 Insucesso: {ins}\n"
        f"• ↩️ Devolução: {dev}\n"
        f"• ❌ Cancelado: {canc}\n"
        f"• ⌛ Atrasados: {n_atrasados}\n"
        f"\n"
        f"📋 *Por Contratante (atrasados — mes)*\n"
        f"{cont_fmt}\n"
        f"\n"
        f"🏢 *Por Polo (atrasados — mes)*\n"
        f"{polo_fmt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    return {"mensagem": msg.strip()}


@app.get("/fechamento")
def fechamento():
    """18h — Consolidado do dia."""
    hoje   = date.today()
    hoje_s = hoje.strftime('%Y-%m-%d')
    mes_s  = hoje.replace(day=1).strftime('%Y-%m-%d')
    data_fmt = pd.Timestamp.now('America/Sao_Paulo').strftime('%d/%m/%Y')

    df_hj  = fetch(hoje_s, hoje_s)
    df_mes = fetch(mes_s, hoje_s)
    if df_hj.empty:
        return {"mensagem": f"Sem dados para fechamento de {hoje_s}"}

    k_hj  = kpis(df_hj)
    k_mes = kpis(df_mes) if not df_mes.empty else k_hj

    sem1h = df_mes[~df_mes['Tipo_SLA'].str.contains('1Hr', na=False)]
    div_count = int(sem1h['Diff_SLA_dias'].notna().sum())
    div_neg   = int((sem1h['Diff_SLA_dias'].fillna(0) < 0).sum())
    div_pos   = int((sem1h['Diff_SLA_dias'].fillna(0) > 0).sum())

    st = status_operacional(df_mes) if not df_mes.empty else {}
    c  = st.get('contagens', {})
    lojas_alert = fmt_lojas_criticas(st.get('lojas_criticas', []))

    polos_fmt   = fmt_polo(k_mes['polos'], top=7)
    grupos_fmt  = fmt_status_grupos(st.get('grupos', {}))
    alerta_fmt  = fmt_alertas(k_mes['cidades_criticas'])

    sla_hj_e  = emoji_sla(k_hj['sla'])
    sla_mes_e = emoji_sla(k_mes['sla'])

    ent_c  = c.get('Entregue', 0)
    tran_c = c.get('Em Trânsito', c.get('Em Transito', 0))
    exp_c  = c.get('Expedindo', 0)
    ins_c  = c.get('Insucesso', 0)
    dev_c  = c.get('Devolução', c.get('Devolucao', 0))
    canc_c = c.get('Cancelado', 0)

    lojas_section = ('\n' + lojas_alert) if lojas_alert else ''

    msg = (
        f"🌆 *FECHAMENTO AGV — {data_fmt} | 18h*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *Dia de hoje*\n"
        f"• Integrados: {k_hj['total']} | D+: {k_hj['sem1h']}\n"
        f"• Entregues: {k_hj['entregues']} | Pendentes: {k_hj['pendentes']}\n"
        f"• Atrasados: {k_hj['atrasados']}\n"
        f"• SLA do dia: {sla_hj_e} *{k_hj['sla']:.1f}%*\n"
        f"\n"
        f"📅 *Acumulado do mes*\n"
        f"• Total D+: {k_mes['sem1h']}\n"
        f"• Entregues: {k_mes['entregues']}\n"
        f"• SLA mes: {sla_mes_e} *{k_mes['sla']:.1f}%*\n"
        f"• Atrasados: {k_mes['atrasados']}\n"
        f"\n"
        f"🏢 *Ranking de Polos (mes)*\n"
        f"{polos_fmt}\n"
        f"\n"
        f"📋 *Status operacional (mes)*\n"
        f"• ✅ Entregues: {ent_c}\n"
        f"• 🚚 Em Trânsito: {tran_c}\n"
        f"• 📬 Expedindo: {exp_c}\n"
        f"• 🔄 Insucesso: {ins_c}\n"
        f"• ↩️ Devolução: {dev_c}\n"
        f"• ❌ Cancelado: {canc_c}\n"
        f"\n"
        f"📊 *Por Contratante (mes)*\n"
        f"{grupos_fmt}"
        f"{lojas_section}\n"
        f"\n"
        f"🔎 *Divergencias SLA (nosso vs sistema)*\n"
        f"• Pedidos com DataSLA diferente: {div_count}\n"
        f"• Sistema mais apertado: {div_neg} | mais folgado: {div_pos}\n"
        f"\n"
        f"{alerta_fmt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    return {"mensagem": msg.strip()}


# ─── START ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

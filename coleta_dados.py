"""Coleta de dados brutos para o projeto de risco de crédito (pessoas físicas, Brasil).

Fontes (todas oficiais e públicas):
  - SCR.data (BCB, dados abertos)   -> dados/raw/scr/
  - Séries SGS (BCB, api.bcb.gov.br) -> dados/raw/sgs_*.csv
  - PNAD Contínua / SIDRA (IBGE)     -> dados/raw/pnadc_desocupacao_uf.csv

Nenhuma limpeza é feita aqui: os arquivos são salvos como vieram da fonte.

Uso:
  python coleta_dados.py scr-listar            # lista arquivos/anos e tamanhos do SCR.data
  python coleta_dados.py scr [--mes 202509]    # baixa 1 mês (padrão: o mais recente)
  python coleta_dados.py sgs
  python coleta_dados.py sidra
  python coleta_dados.py tudo [--mes 202509]   # scr + sgs + sidra + resumo
  python coleta_dados.py resumo
"""
import argparse
import csv
import io
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RAW = Path(__file__).resolve().parent / "dados" / "raw"
TIMEOUT = (10, 60)  # (conexão, leitura) em segundos
LIMITE_MB = 500
INICIO = date(2012, 1, 1)

SCR_PACOTE = "https://dadosabertos.bcb.gov.br/api/3/action/package_show?id=scr_data"

# Códigos do SGS/BCB (conferir nome/periodicidade no portal SGS)
SERIES_SGS = {
    "selic_meta": 432,        # Taxa de juros - Meta Selic definida pelo Copom (% a.a.), diária
    "selic_acum_mes": 4390,   # Taxa de juros - Selic acumulada no mês (%), mensal
    "ipca_mensal": 433,       # IPCA - variação mensal (%), mensal
    "dolar_venda": 1,         # Taxa de câmbio - Livre - Dólar americano (venda), diária
}

# SIDRA tabela 4099: taxa de desocupação, PNAD Contínua trimestral (14 anos ou mais)
# t=tabela, n3=UF, v=variável 4099 (taxa de desocupação), p=todos os trimestres
SIDRA_URL = "https://apisidra.ibge.gov.br/values/t/4099/n3/all/v/4099/p/all"


def sessao():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "projeto-ebac-risco-credito/1.0"
    return s


def get(s, url, **kw):
    try:
        r = s.get(url, timeout=TIMEOUT, **kw)
        r.raise_for_status()
        return r
    except requests.exceptions.RequestException as e:
        sys.exit(f"ERRO ao acessar {url}\n  {type(e).__name__}: {e}")


# ----------------------------------------------------------------------------- SCR.data
def scr_recursos(s):
    """Lista os recursos (arquivos) do dataset scr_data via API CKAN."""
    pacote = get(s, SCR_PACOTE).json()
    if not pacote.get("success"):
        sys.exit(f"CKAN respondeu sem sucesso: {pacote}")
    saida = []
    for r in pacote["result"]["resources"]:
        url = r.get("url") or ""
        tamanho = r.get("size")
        if not tamanho:  # CKAN nem sempre informa; tenta HEAD
            try:
                h = s.head(url, timeout=TIMEOUT, allow_redirects=True)
                tamanho = int(h.headers.get("Content-Length", 0)) or None
            except requests.exceptions.RequestException:
                tamanho = None
        achou = re.search(r"(20\d{2})[-_]?(0[1-9]|1[0-2])", f"{r.get('name', '')} {url}")
        saida.append({
            "nome": r.get("name"), "formato": r.get("format"), "url": url,
            "mb": round(int(tamanho) / 1e6, 1) if tamanho else None,
            "mes": f"{achou.group(1)}{achou.group(2)}" if achou else None,
            "atualizado": r.get("last_modified") or r.get("created"),
        })
    return saida


def scr_listar(s):
    rec = scr_recursos(s)
    df = pd.DataFrame(rec)
    print(f"\nSCR.data: {len(df)} recursos")
    with pd.option_context("display.width", 200, "display.max_colwidth", 70, "display.max_rows", 500):
        print(df[["nome", "formato", "mes", "mb", "atualizado"]].to_string(index=False))
    meses = sorted(m for m in df["mes"].dropna().unique())
    if meses:
        print(f"\nMeses identificados: {meses[0]} a {meses[-1]}  (anos: {sorted({m[:4] for m in meses})})")
    return rec


def baixar_limitado(s, url, destino, limite_mb):
    """Baixa em streaming; aborta se passar do limite (também quando não há Content-Length)."""
    r = get(s, url, stream=True)
    total = int(r.headers.get("Content-Length", 0))
    if total > limite_mb * 1e6:
        sys.exit(f"ABORTADO: {url} tem {total / 1e6:.0f} MB (> {limite_mb} MB). "
                 f"Rode com --limite-mb maior se quiser mesmo baixar.")
    lido = 0
    with open(destino, "wb") as f:
        for bloco in r.iter_content(1 << 20):
            lido += len(bloco)
            if lido > limite_mb * 1e6:
                f.close()
                destino.unlink(missing_ok=True)
                sys.exit(f"ABORTADO: download passou de {limite_mb} MB.")
            f.write(bloco)
    return destino


def scr_baixar(s, mes, limite_mb):
    rec = scr_recursos(s)
    com_mes = [r for r in rec if r["mes"]]
    if not com_mes:
        sys.exit("Nenhum recurso com mês no nome/URL; veja `scr-listar` e ajuste a seleção.")
    mes = mes or max(r["mes"] for r in com_mes)
    escolhidos = [r for r in com_mes if r["mes"] == mes]
    if not escolhidos:
        sys.exit(f"Mês {mes} não encontrado. Disponíveis: {sorted({r['mes'] for r in com_mes})}")
    r = escolhidos[0]
    print(f"Baixando {r['nome']} ({r['mb']} MB) <- {r['url']}")
    pasta = RAW / "scr"
    pasta.mkdir(parents=True, exist_ok=True)
    arq = baixar_limitado(s, r["url"], pasta / Path(r["url"]).name, limite_mb)
    if zipfile.is_zipfile(arq):
        with zipfile.ZipFile(arq) as z:
            alvo = sum(i.file_size for i in z.infolist())
            if alvo > limite_mb * 1e6 * 4:
                sys.exit(f"ABORTADO: conteúdo do zip descompactado ocupa {alvo / 1e6:.0f} MB.")
            z.extractall(pasta)
        arq.unlink()
    print(f"Salvo em {pasta}")


# ----------------------------------------------------------------------------- SGS
def sgs_baixar(s):
    RAW.mkdir(parents=True, exist_ok=True)
    hoje = date.today()
    for nome, cod in SERIES_SGS.items():
        # API limita consultas de séries diárias a janelas de 10 anos: busca em blocos
        partes, ini = [], INICIO
        while ini <= hoje:
            fim = min(date(ini.year + 9, 12, 31), hoje)
            url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{cod}/dados?formato=json"
                   f"&dataInicial={ini:%d/%m/%Y}&dataFinal={fim:%d/%m/%Y}")
            dados = get(s, url).json()
            if isinstance(dados, list):
                partes.extend(dados)
            ini = date(fim.year + 1, 1, 1)
        if not partes:
            print(f"AVISO: série {cod} ({nome}) veio vazia")
            continue
        df = pd.DataFrame(partes)
        df.to_csv(RAW / f"sgs_{nome}.csv", index=False)
        print(f"sgs {cod} ({nome}): {len(df)} linhas")


# ----------------------------------------------------------------------------- SIDRA
def sidra_baixar(s):
    RAW.mkdir(parents=True, exist_ok=True)
    dados = get(s, SIDRA_URL).json()
    if not isinstance(dados, list) or len(dados) < 2:
        sys.exit(f"Resposta inesperada do SIDRA: {str(dados)[:300]}")
    # a 1ª linha do JSON do SIDRA é o cabeçalho (códigos -> rótulos)
    df = pd.DataFrame(dados[1:]).rename(columns=dados[0])
    df.to_csv(RAW / "pnadc_desocupacao_uf.csv", index=False)
    print(f"sidra t4099: {len(df)} linhas")


# ----------------------------------------------------------------------------- resumo
def abrir_csv(path, **kw):
    amostra = path.read_bytes()[:65536]
    for enc in ("utf-8-sig", "latin-1"):
        try:
            texto = amostra.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        sep = csv.Sniffer().sniff(texto.splitlines()[0], delimiters=";,|\t").delimiter
    except csv.Error:
        sep = ","
    return pd.read_csv(path, sep=sep, encoding=enc, dtype=str, **kw)


COLUNAS_PERIODO = ("data", "data_base", "Trimestre (Código)", "Mês (Código)")


def resumo():
    arquivos = sorted(p for p in RAW.rglob("*.csv"))
    if not arquivos:
        print("Nenhum CSV em", RAW)
        return
    linhas = []
    for p in arquivos:
        n, cols, ini, fim, col_p = 0, None, None, None, None
        for ch in abrir_csv(p, chunksize=500_000):
            if cols is None:
                cols = list(ch.columns)
                col_p = next((c for c in COLUNAS_PERIODO if c in cols), None)
            n += len(ch)
            if col_p:
                v = ch[col_p].dropna()
                if col_p == "data":  # dd/mm/aaaa
                    v = pd.to_datetime(v, format="%d/%m/%Y", errors="coerce").dropna()
                if len(v):
                    ini = v.min() if ini is None else min(ini, v.min())
                    fim = v.max() if fim is None else max(fim, v.max())
        fmt = lambda x: x.strftime("%Y-%m-%d") if hasattr(x, "strftime") else x
        linhas.append({"arquivo": str(p.relative_to(RAW.parent.parent)), "linhas": n,
                       "colunas": len(cols or []), "período": f"{fmt(ini)} a {fmt(fim)}" if ini else "n/d",
                       "nomes_colunas": ", ".join(cols or [])})
    with pd.option_context("display.width", 250, "display.max_colwidth", 90):
        print("\n=== RESUMO ===")
        print(pd.DataFrame(linhas).to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("etapa", choices=["scr-listar", "scr", "sgs", "sidra", "tudo", "resumo"])
    ap.add_argument("--mes", help="AAAAMM do SCR.data (padrão: mais recente)")
    ap.add_argument("--limite-mb", type=float, default=LIMITE_MB)
    a = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    s = sessao()
    if a.etapa == "scr-listar":
        scr_listar(s)
    if a.etapa in ("scr", "tudo"):
        scr_baixar(s, a.mes, a.limite_mb)
    if a.etapa in ("sgs", "tudo"):
        sgs_baixar(s)
    if a.etapa in ("sidra", "tudo"):
        sidra_baixar(s)
    if a.etapa in ("tudo", "resumo", "scr", "sgs", "sidra"):
        resumo()


if __name__ == "__main__":
    main()

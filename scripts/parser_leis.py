import hashlib
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
MODO = os.environ.get("MODO_EXECUCAO", "verificar_todas")
LEI_ID_ALVO = os.environ.get("LEI_ID_ALVO", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

HEADERS_HTTP = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

REGEX_CABECALHO_ESTRUTURA = re.compile(
    r"^(LIVRO|PARTE|T[ÍI]TULO|CAP[ÍI]TULO|SE[ÇC][ÃA]O|SUBSE[ÇC][ÃA]O)\s+"
    r"(?:[IVXLCDM0-9A-Z-]+|ÚNICO|GERAL|PRELIMINAR|ESPECIAL|COMPLEMENTAR)"
    r"(?:\s+(?:ÚNICO|GERAL|PRELIMINAR|ESPECIAL|COMPLEMENTAR))?$",
    re.IGNORECASE,
)


def normalizar_texto(txt: str) -> str:
    txt = txt.replace("\xa0", " ").replace("\r", " ")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def slug_seguro(txt: str) -> str:
    nfkd = unicodedata.normalize("NFKD", txt)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    limpo = re.sub(r"[^A-Za-z0-9]+", "_", ascii_str).strip("_").upper()
    return limpo[:60] if limpo else "ITEM"


def baixar_html_oficial(url: str) -> str:
    resp = requests.get(url, headers=HEADERS_HTTP, timeout=45)
    resp.raise_for_status()

    # Planalto costuma servir iso-8859-1 / windows-1252 ou utf-8
    conteudo_bytes = resp.content
    for enc in ("utf-8", "windows-1252", "iso-8859-1"):
        try:
            texto = conteudo_bytes.decode(enc)
            if "Art." in texto or "art." in texto:
                return texto
        except UnicodeDecodeError:
            continue
    return resp.text


def linha_parece_descricao_estrutura(linha: str) -> bool:
    if not linha or len(linha) > 220:
        return False
    if re.match(r"^(Arts?\.|§|Parágrafo\s+único|[IVXLCDM]+\s*[-–—]|[a-z]\)|\"|“|‘)", linha, re.IGNORECASE):
        return False
    if linha.endswith((";", ":")):
        return False
    if REGEX_CABECALHO_ESTRUTURA.match(linha):
        return False
    return True


def extrair_dispositivos(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "head", "nav", "footer"]):
        tag.decompose()

    # Remove trechos riscados (strike/s/del) comuns no Planalto
    for riscado in soup.find_all(["strike", "s", "del"]):
        riscado.decompose()

    blocos_brutos = []
    for el in soup.find_all(["p", "div", "h1", "h2", "h3", "h4"]):
        if el.find(["p", "div"]):
            continue
        txt = normalizar_texto(el.get_text(" ", strip=True))
        if txt:
            blocos_brutos.append(txt)

    if not blocos_brutos:
        texto_geral = soup.get_text("\n")
        blocos_brutos = [normalizar_texto(l) for l in texto_geral.splitlines() if normalizar_texto(l)]

    # Une cabeçalhos estruturais ("TÍTULO I") à sua descrição na linha imediatamente seguinte ("Dos Princípios Fundamentais")
    blocos_consolidados = []
    idx = 0
    while idx < len(blocos_brutos):
        atual = blocos_brutos[idx]
        if REGEX_CABECALHO_ESTRUTURA.match(atual) and (idx + 1) < len(blocos_brutos):
            seguinte = blocos_brutos[idx + 1]
            if linha_parece_descricao_estrutura(seguinte):
                blocos_consolidados.append(f"{atual} — {seguinte}")
                idx += 2
                continue
        blocos_consolidados.append(atual)
        idx += 1

    dispositivos = []
    art_atual = "PREAMBULO"
    par_atual = ""
    inc_atual = ""
    cont_estrutura = 0
    ordem = 0
    iniciou_artigos = False

    re_estrutura = re.compile(
        r"^(LIVRO|PARTE|T[ÍI]TULO|CAP[ÍI]TULO|SE[ÇC][ÃA]O|SUBSE[ÇC][ÃA]O)\b",
        re.IGNORECASE,
    )
    re_artigo = re.compile(r"^(Arts?\.?\s*\d+(?:-[A-Za-z])?[º°o.]?)\s*(.*)$", re.IGNORECASE)
    re_paragrafo = re.compile(r"^(§\s*\d+[º°o.]?|Parágrafo\s+único\.?)\s*(.*)$", re.IGNORECASE)
    re_inciso = re.compile(r"^([IVXLCDM]+\s*[-–—])\s*(.*)$")
    re_alinea = re.compile(r"^([a-z]\))\s*(.*)$")

    for linha in blocos_consolidados:
        if re_estrutura.match(linha):
            cont_estrutura += 1
            ordem += 1
            slug = f"EST_{cont_estrutura}_{slug_seguro(linha)}"
            dispositivos.append({
                "slug": slug,
                "tipo": "estrutura",
                "rotulo": "",
                "texto": linha,
                "ordem": ordem,
            })
            continue

        m_art = re_artigo.match(linha)
        if m_art:
            iniciou_artigos = True
            rotulo = m_art.group(1).strip()
            texto = m_art.group(2).strip()
            num_art = re.sub(r"^Arts?\.?\s*", "", rotulo, flags=re.IGNORECASE)
            num_art = re.sub(r"[º°o.]", "", num_art).strip().upper()
            art_atual = f"ART_{num_art}"
            par_atual = ""
            inc_atual = ""
            ordem += 1
            dispositivos.append({
                "slug": art_atual,
                "tipo": "artigo",
                "rotulo": rotulo,
                "texto": texto,
                "ordem": ordem,
            })
            continue

        if not iniciou_artigos:
            continue

        m_par = re_paragrafo.match(linha)
        if m_par:
            rotulo = m_par.group(1).strip()
            texto = m_par.group(2).strip()
            num_par = "UNICO" if "único" in rotulo.lower() else re.sub(r"[^0-9A-Za-z]", "", rotulo)
            par_atual = f"PAR_{num_par}"
            inc_atual = ""
            ordem += 1
            dispositivos.append({
                "slug": f"{art_atual}_{par_atual}",
                "tipo": "paragrafo",
                "rotulo": rotulo,
                "texto": texto,
                "ordem": ordem,
            })
            continue

        m_inc = re_inciso.match(linha)
        if m_inc:
            rotulo = m_inc.group(1).strip()
            texto = m_inc.group(2).strip()
            romano = re.sub(r"[^IVXLCDM]", "", rotulo.upper())
            inc_atual = f"INC_{romano}"
            prefixo = f"{art_atual}_{par_atual}" if par_atual else art_atual
            ordem += 1
            dispositivos.append({
                "slug": f"{prefixo}_{inc_atual}",
                "tipo": "inciso",
                "rotulo": rotulo,
                "texto": texto,
                "ordem": ordem,
            })
            continue

        m_ali = re_alinea.match(linha)
        if m_ali:
            rotulo = m_ali.group(1).strip()
            texto = m_ali.group(2).strip()
            letra = rotulo[0].upper()
            partes = [art_atual]
            if par_atual:
                partes.append(par_atual)
            if inc_atual:
                partes.append(inc_atual)
            partes.append(f"ALI_{letra}")
            ordem += 1
            dispositivos.append({
                "slug": "_".join(partes),
                "tipo": "alinea",
                "rotulo": rotulo,
                "texto": texto,
                "ordem": ordem,
            })
            continue

    # Garante unicidade de slugs caso a lei tenha numeração duplicada
    vistos = {}
    for d in dispositivos:
        s = d["slug"]
        if s in vistos:
            vistos[s] += 1
            d["slug"] = f"{s}_{vistos[s]}"
        else:
            vistos[s] = 1

    return dispositivos


def calcular_hash(dispositivos: list[dict]) -> str:
    base = "\n".join(f"{d['slug']}|{d['rotulo']}|{d['texto']}" for d in dispositivos)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def inserir_em_lotes(lei_id: str, dispositivos: list[dict], status: str):
    registros = [
        {
            "lei_id": lei_id,
            "slug": d["slug"],
            "tipo": d["tipo"],
            "rotulo": d["rotulo"],
            "texto": d["texto"],
            "ordem": d["ordem"],
            "versao_status": status,
        }
        for d in dispositivos
    ]
    for i in range(0, len(registros), 500):
        supabase.table("dispositivos").insert(registros[i : i + 500]).execute()


def processar_lei(lei: dict, forcar_reestruturacao: bool = False):
    lei_id = lei["id"]
    url = lei["url"]
    print(f"Processando [{lei['titulo']}] ({url})...")

    try:
        supabase.table("leis").update({"status_extracao": "processando"}).eq("id", lei_id).execute()
        html = baixar_html_oficial(url)
        dispositivos = extrair_dispositivos(html)

        if not dispositivos:
            raise ValueError("Nenhum dispositivo extraído da página.")

        novo_hash = calcular_hash(dispositivos)
        hash_atual = lei.get("hash_atual")

        # Primeira extração OU reestruturação direta dos índices (preserva todas as anotações e grifos por slug)
        if not hash_atual or forcar_reestruturacao:
            supabase.table("dispositivos").delete().eq("lei_id", lei_id).eq("versao_status", "vigente").execute()
            inserir_em_lotes(lei_id, dispositivos, "vigente")
            supabase.table("leis").update({
                "hash_atual": novo_hash,
                "status_extracao": "concluido",
                "ultima_verificacao": datetime.now(timezone.utc).isoformat(),
            }).eq("id", lei_id).execute()
            print(f" -> Estrutura/índice atualizado ({len(dispositivos)} dispositivos).")
            return

        # Verificação de alteração legislativa
        if novo_hash != hash_atual:
            supabase.table("dispositivos").delete().eq("lei_id", lei_id).eq("versao_status", "pendente").execute()
            inserir_em_lotes(lei_id, dispositivos, "pendente")
            supabase.table("leis").update({
                "hash_pendente": novo_hash,
                "tem_atualizacao": True,
                "status_extracao": "concluido",
                "ultima_verificacao": datetime.now(timezone.utc).isoformat(),
            }).eq("id", lei_id).execute()
            print(" -> Alteração legislativa detectada e salva como pendente.")
        else:
            supabase.table("leis").update({
                "status_extracao": "concluido",
                "ultima_verificacao": datetime.now(timezone.utc).isoformat(),
            }).eq("id", lei_id).execute()
            print(" -> Sem alterações na fonte oficial.")

    except Exception as e:
        print(f" -> Erro ao processar {lei['titulo']}: {e}", file=sys.stderr)
        supabase.table("leis").update({"status_extracao": "erro"}).eq("id", lei_id).execute()


def main():
    if MODO in ("verificar_lei", "reprocessar_indice_lei") and LEI_ID_ALVO:
        resp = supabase.table("leis").select("*").eq("id", LEI_ID_ALVO).execute()
        leis = resp.data or []
        for lei in leis:
            processar_lei(lei, forcar_reestruturacao=(MODO == "reprocessar_indice_lei"))
        return

    if MODO == "reprocessar_todos_indices":
        resp = supabase.table("leis").select("*").execute()
        for lei in (resp.data or []):
            processar_lei(lei, forcar_reestruturacao=True)
        return

    if MODO == "extrair_pendentes":
        resp = supabase.table("leis").select("*").eq("status_extracao", "pendente").execute()
    else:
        resp = supabase.table("leis").select("*").execute()

    for lei in (resp.data or []):
        processar_lei(lei, forcar_reestruturacao=False)


if __name__ == "__main__":
    main()

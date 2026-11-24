import hashlib
import os
import re
import sys
import time
import traceback
import unicodedata
from datetime import datetime, timezone
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup, NavigableString
from supabase import create_client, Client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
MODO = os.environ.get("MODO_EXECUCAO", "verificar_todas")
LEI_ID_ALVO = os.environ.get("LEI_ID_ALVO", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

REGEX_CABECALHO_ESTRUTURA = re.compile(
    r"^(LIVRO|PARTE|T[ÍI]TULO|CAP[ÍI]TULO|SE[ÇC][ÃA]O|SUBSE[ÇC][ÃA]O)\s+"
    r"(?:[IVXLCDM0-9A-Z-]+|ÚNICO|GERAL|PRELIMINAR|ESPECIAL|COMPLEMENTAR)"
    r"(?:\s+(?:ÚNICO|GERAL|PRELIMINAR|ESPECIAL|COMPLEMENTAR))?$",
    re.IGNORECASE,
)


def normalizar_texto(txt: str) -> str:
    txt = txt.replace("\xa0", " ").replace("\u200b", "").replace("\r", " ")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def slug_seguro(txt: str) -> str:
    nfkd = unicodedata.normalize("NFKD", txt)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    limpo = re.sub(r"[^A-Za-z0-9]+", "_", ascii_str).strip("_").upper()
    return limpo[:60] if limpo else "ITEM"


def decodificar_bytes(conteudo_bytes: bytes) -> str:
    for enc in ("utf-8", "windows-1252", "iso-8859-1"):
        try:
            texto = conteudo_bytes.decode(enc)
            if "Art." in texto or "art." in texto:
                return texto
        except UnicodeDecodeError:
            continue
    return conteudo_bytes.decode("utf-8", errors="ignore")


def baixar_via_playwright(url: str) -> str:
    from playwright.sync_api import sync_playwright

    print("   [2/3] Acionando navegador Chromium real (Playwright)...", file=sys.stderr)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(4000)
        html = page.content()
        browser.close()
        return html


def baixar_via_proxy_leitura(url: str) -> str:
    print("   [3/3] Acionando proxy de leitura...", file=sys.stderr)
    url_proxy = f"https://r.jina.ai/{url}"
    resp = cffi_requests.get(url_proxy, impersonate="chrome124", timeout=90)
    if resp.status_code == 200 and ("Art." in resp.text or "art." in resp.text):
        linhas = []
        for l in resp.text.splitlines():
            limpa = l.strip()
            # Remove marcações de tachado Markdown (~~texto revogado~~) e links de navegação
            if not limpa or (limpa.startswith("~~") and limpa.endswith("~~")):
                continue
            limpa = re.sub(r"~~.*?~~", "", limpa).strip()
            limpa = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", limpa).strip()
            if limpa:
                linhas.append(f"<p>{limpa}</p>")
        return "\n".join(linhas)
    raise RuntimeError(f"Proxy retornou status {resp.status_code}")


def baixar_html_oficial(url: str) -> str:
    url_limpa = url.strip().replace(
        "http://www.planalto.gov.br", "https://www.planalto.gov.br"
    )

    for perfil in ("chrome124", "chrome120"):
        try:
            resp = cffi_requests.get(
                url_limpa,
                impersonate=perfil,
                timeout=60,
                verify=False,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            print(
                f"   [1/3] curl_cffi ({perfil}) -> HTTP {resp.status_code} ({len(resp.content)} bytes)",
                file=sys.stderr,
            )
            if resp.status_code == 200:
                html = decodificar_bytes(resp.content)
                if "Art." in html or "art." in html:
                    return html
        except Exception as err:
            print(f"   [1/3] Perfil {perfil} falhou: {err}", file=sys.stderr)
            time.sleep(2)

    try:
        html_browser = baixar_via_playwright(url_limpa)
        if "Art." in html_browser or "art." in html_browser:
            return html_browser
    except Exception as err:
        print(f"   [2/3] Playwright falhou: {err}", file=sys.stderr)

    return baixar_via_proxy_leitura(url_limpa)


def linha_parece_descricao_estrutura(linha: str) -> bool:
    if not linha or len(linha) > 220:
        return False
    if re.match(
        r"^(Arts?\.|§|Parágrafo\s+único|[IVXLCDM]+\s*[-–—]|[a-z]\)|\"|“|‘)",
        linha,
        re.IGNORECASE,
    ):
        return False
    if linha.endswith((";", ":")):
        return False
    if REGEX_CABECALHO_ESTRUTURA.match(linha):
        return False
    return True


def extrair_blocos_texto(soup: BeautifulSoup) -> list[str]:
    area_principal = (
        soup.find(id="conteudo")
        or soup.find(class_=re.compile(r"(texto[-_]?norma|norma[-_]?texto|conteudo[-_]?norma)", re.I))
        or soup.body
        or soup
    )

    tags_bloco = {
        "p", "div", "li", "h1", "h2", "h3", "h4", "h5",
        "td", "th", "blockquote", "center", "section", "article"
    }

    blocos = []
    buffer_inline = []

    def descarregar_buffer():
        if not buffer_inline:
            return
        bruto = "".join(buffer_inline)
        buffer_inline.clear()
        for parte in bruto.splitlines():
            txt = normalizar_texto(parte)
            if txt:
                blocos.append(txt)

    def percorrer(no):
        if isinstance(no, NavigableString):
            buffer_inline.append(str(no))
            return
        nome = getattr(no, "name", None)
        if not nome:
            return
        if nome == "br":
            buffer_inline.append("\n")
            return
        eh_bloco = nome in tags_bloco
        if eh_bloco:
            descarregar_buffer()
        for filho in no.children:
            percorrer(filho)
        if eh_bloco:
            descarregar_buffer()

    percorrer(area_principal)
    descarregar_buffer()
    return blocos


def extrair_dispositivos(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "head", "nav", "footer", "header", "aside"]):
        tag.decompose()

    for riscado in soup.find_all(["strike", "s", "del"]):
        riscado.decompose()

    blocos_brutos = extrair_blocos_texto(soup)

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
    re_artigo = re.compile(
        r"^(Arts?\.?\s*\d+(?:-[A-Za-z])?[º°o.]?)\s*(.*)$", re.IGNORECASE
    )
    re_paragrafo = re.compile(
        r"^(§\s*\d+[º°o.]?|Parágrafo\s+único\.?)\s*(.*)$", re.IGNORECASE
    )
    re_inciso = re.compile(r"^([IVXLCDM]+\s*[-–—])\s*(.*)$")
    re_alinea = re.compile(r"^([a-z]\))\s*(.*)$")

    for linha in blocos_consolidados:
        if re_estrutura.match(linha):
            cont_estrutura += 1
            ordem += 1
            slug = f"EST_{cont_estrutura}_{slug_seguro(linha)}"
            dispositivos.append(
                {
                    "slug": slug,
                    "tipo": "estrutura",
                    "rotulo": "",
                    "texto": linha,
                    "ordem": ordem,
                }
            )
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
            dispositivos.append(
                {
                    "slug": art_atual,
                    "tipo": "artigo",
                    "rotulo": rotulo,
                    "texto": texto,
                    "ordem": ordem,
                }
            )
            continue

        if not iniciou_artigos:
            continue

        m_par = re_paragrafo.match(linha)
        if m_par:
            rotulo = m_par.group(1).strip()
            texto = m_par.group(2).strip()
            num_par = (
                "UNICO"
                if "único" in rotulo.lower()
                else re.sub(r"[^0-9A-Za-z]", "", rotulo)
            )
            par_atual = f"PAR_{num_par}"
            inc_atual = ""
            ordem += 1
            dispositivos.append(
                {
                    "slug": f"{art_atual}_{par_atual}",
                    "tipo": "paragrafo",
                    "rotulo": rotulo,
                    "texto": texto,
                    "ordem": ordem,
                }
            )
            continue

        m_inc = re_inciso.match(linha)
        if m_inc:
            rotulo = m_inc.group(1).strip()
            texto = m_inc.group(2).strip()
            romano = re.sub(r"[^IVXLCDM]", "", rotulo.upper())
            inc_atual = f"INC_{romano}"
            prefixo = f"{art_atual}_{par_atual}" if par_atual else art_atual
            ordem += 1
            dispositivos.append(
                {
                    "slug": f"{prefixo}_{inc_atual}",
                    "tipo": "inciso",
                    "rotulo": rotulo,
                    "texto": texto,
                    "ordem": ordem,
                }
            )
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
            dispositivos.append(
                {
                    "slug": "_".join(partes),
                    "tipo": "alinea",
                    "rotulo": rotulo,
                    "texto": texto,
                    "ordem": ordem,
                }
            )
            continue

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


def inserir_em_lotes(lei_id: str, user_id: str, dispositivos: list[dict], status: str):
    registros = [
        {
            "lei_id": lei_id,
            "user_id": user_id,
            "slug": d["slug"],
            "tipo": d["tipo"],
            "rotulo": d["rotulo"],
            "texto": d["texto"],
            "ordem": d["ordem"],
            "versao_status": status,
        }
        for d in dispositivos
    ]
    for i in range(0, len(registros), 80):
        supabase.table("dispositivos").insert(registros[i : i + 80]).execute()


def processar_lei(lei: dict, forcar_reestruturacao: bool = False):
    lei_id = lei["id"]
    user_id = lei["user_id"]
    url = lei["url"]
    print(f"Processando [{lei['titulo']}] ({url})...")

    try:
        supabase.table("leis").update({"status_extracao": "processando"}).eq(
            "id", lei_id
        ).execute()
        html = baixar_html_oficial(url)
        dispositivos = extrair_dispositivos(html)
        print(f"   Extraídos {len(dispositivos)} dispositivos da fonte.")

        if not dispositivos:
            raise ValueError("Nenhum dispositivo extraído da página.")

        novo_hash = calcular_hash(dispositivos)
        hash_atual = lei.get("hash_atual")

        resp_exist = (
            supabase.table("dispositivos")
            .select("id", count="exact")
            .eq("lei_id", lei_id)
            .eq("versao_status", "vigente")
            .limit(1)
            .execute()
        )
        qtd_existentes = resp_exist.count or 0
        sem_vigentes = qtd_existentes == 0

        # Proteção: impede que uma raspagem truncada (ex: menos de 40% do tamanho atual) sobrescreva a lei inteira
        if qtd_existentes >= 15 and len(dispositivos) < int(qtd_existentes * 0.4):
            raise ValueError(
                f"Extração incompleta detectada ({len(dispositivos)} dispositivos extraídos contra {qtd_existentes} vigentes). Abortando para proteger a lei."
            )

        if not hash_atual or forcar_reestruturacao or sem_vigentes:
            supabase.table("dispositivos").delete().eq("lei_id", lei_id).eq(
                "versao_status", "reestruturando"
            ).execute()
            inserir_em_lotes(lei_id, user_id, dispositivos, "reestruturando")

            supabase.table("dispositivos").delete().eq("lei_id", lei_id).eq(
                "versao_status", "vigente"
            ).execute()
            supabase.table("dispositivos").update({"versao_status": "vigente"}).eq(
                "lei_id", lei_id
            ).eq("versao_status", "reestruturando").execute()

            supabase.table("leis").update(
                {
                    "hash_atual": novo_hash,
                    "hash_pendente": None,
                    "tem_atualizacao": False,
                    "status_extracao": "concluido",
                    "ultima_verificacao": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", lei_id).execute()
            print(
                f" -> Estrutura/índice atualizado com sucesso ({len(dispositivos)} dispositivos)."
            )
            return

        if novo_hash != hash_atual:
            supabase.table("dispositivos").delete().eq("lei_id", lei_id).eq(
                "versao_status", "pendente"
            ).execute()
            inserir_em_lotes(lei_id, user_id, dispositivos, "pendente")
            supabase.table("leis").update(
                {
                    "hash_pendente": novo_hash,
                    "tem_atualizacao": True,
                    "status_extracao": "concluido",
                    "ultima_verificacao": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", lei_id).execute()
            print(" -> Alteração legislativa detectada e salva como pendente.")
        else:
            supabase.table("leis").update(
                {
                    "status_extracao": "concluido",
                    "ultima_verificacao": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", lei_id).execute()
            print(" -> Sem alterações na fonte oficial.")

    except Exception as e:
        print(f" -> Erro ao processar {lei['titulo']}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        supabase.table("dispositivos").delete().eq("lei_id", lei_id).eq(
            "versao_status", "reestruturando"
        ).execute()
        supabase.table("leis").update({"status_extracao": "erro"}).eq(
            "id", lei_id
        ).execute()


def main():
    if MODO in ("verificar_lei", "reprocessar_indice_lei") and LEI_ID_ALVO:
        resp = supabase.table("leis").select("*").eq("id", LEI_ID_ALVO).execute()
        for lei in resp.data or []:
            processar_lei(
                lei, forcar_reestruturacao=(MODO == "reprocessar_indice_lei")
            )
        return

    if MODO == "reprocessar_todos_indices":
        resp = supabase.table("leis").select("*").execute()
        for lei in resp.data or []:
            processar_lei(lei, forcar_reestruturacao=True)
        return

    if MODO == "extrair_pendentes":
        resp = (
            supabase.table("leis")
            .select("*")
            .eq("status_extracao", "pendente")
            .execute()
        )
    else:
        resp = supabase.table("leis").select("*").execute()

    for lei in resp.data or []:
        processar_lei(lei, forcar_reestruturacao=False)


if __name__ == "__main__":
    main()

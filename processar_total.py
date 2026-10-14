#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
processar_total.py  (v2)

Lê prova_N.pdf + gabarito_N.pdf (na pasta atual ou em --pasta) e gera questoes.json.

O que mudou em relação à v1:
  * Leitura por COLUNAS: detecta automaticamente páginas de 2 colunas (e páginas mistas),
    em vez de misturar as linhas das duas colunas.
  * Cabeçalho/rodapé/marca d'água removidos por REPETIÇÃO entre páginas (não por lista fixa de sites).
  * Marcadores de questão detectados automaticamente ("Questão 3", "3.", "03)", "03"...),
    validados por sequência 1,2,3... para não confundir com itens dentro do enunciado.
  * Alternativas detectadas como cadeia A→B→C→D(→E) (formatos "A)", "(A)", "A.", "A -").
  * Matéria: faixas "Língua Portuguesa – Questões 1 a 10" quando existirem, senão títulos em
    caixa-alta entre as questões. Texto de apoio ("Texto 1 ...") fica em campo separado.
  * Gabarito consolidado: o PDF é dividido em blocos (cada reinício da numeração em 1) e cada bloco
    é pontuado contra a identidade da prova (cargo, tipo, cor) + quantidade de respostas.
    Suporta lista "01 A", tabela com colunas por tipo/cor e formato transposto (linha de números + linha de letras).
  * Relatório no final listando o que ficou duvidoso (sem gabarito, sem alternativas, etc).

Uso:
    python processar_total.py
    python processar_total.py --debug          # grava debug/ com o texto limpo de cada PDF
    python processar_total.py --pasta "C:\\Users\\voce\\Downloads\\provas_concurso"
"""
import argparse
import glob
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter

import pdfplumber

# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s):
    return re.sub(r"\s+", " ", sem_acento(s).upper()).strip()


def chave_repeticao(s):
    return re.sub(r"\d+", "#", norm(s))


COR_RE = re.compile(r"\b(BRANC[AO]|AMAREL[AO]|AZUL|VERDE|ROSA|CINZA|LARANJA|VERMELH[AO]|PRET[AO]|LILAS|ROX[AO]|MARROM)\b")
TIPO_RE = re.compile(r"\b(?:TIPO|VERSAO|MODELO|CADERNO)\s*[:\-]?\s*(\d{1,2}|[A-D])\b")


def cor_canon(c):
    return re.sub(r"[AO]$", "", c)


# ---------------------------------------------------------------------------
# Leitura do PDF (colunas + linhas)
# ---------------------------------------------------------------------------


def _agrupar_linhas(words):
    if not words:
        return []
    alturas = sorted(w["bottom"] - w["top"] for w in words)
    tol = max(2.0, 0.5 * alturas[len(alturas) // 2])
    linhas = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if linhas and abs(w["top"] - linhas[-1]["top"]) <= tol:
            linhas[-1]["words"].append(w)
        else:
            linhas.append({"top": w["top"], "words": [w]})
    for L in linhas:
        L["words"].sort(key=lambda w: w["x0"])
        L["texto"] = " ".join(w["text"] for w in L["words"])
        L["bottom"] = max(w["bottom"] for w in L["words"])
    return linhas


def _detectar_calha(words, largura):
    """Procura uma 'calha' vertical (espaço entre colunas). Retorna o x dela ou None."""
    if len(words) < 40:
        return None
    linhas = _agrupar_linhas(words)
    melhor = None
    for x in range(int(largura * 0.30), int(largura * 0.70), 2):
        cruza = ambos = 0
        for L in linhas:
            ws = L["words"]
            if any(w["x0"] < x < w["x1"] for w in ws):
                cruza += 1
            elif ws[0]["x1"] <= x and ws[-1]["x0"] >= x:
                ambos += 1
        total = cruza + ambos
        if total < 8:
            continue
        cand = (cruza / total, abs(x - largura / 2), x)
        if melhor is None or cand < melhor:
            melhor = cand
    if not melhor or melhor[0] > 0.25:
        return None
    x = melhor[2]
    esq = sum(1 for w in words if (w["x0"] + w["x1"]) / 2 < x)
    if esq < 0.2 * len(words) or esq > 0.8 * len(words):
        return None
    return x


def linhas_da_pagina(page, usar_colunas=True):
    # ignora texto girado (marcas d'água diagonais/verticais tipo "www.pciconcursos.com.br").
    # Obs.: o campo "upright" do pdfminer é True até para texto a 45°, por isso olhamos a matriz do caractere.
    def _reto(o):
        if o.get("object_type") != "char":
            return True
        m = o.get("matrix")
        if m:
            return abs(m[1]) < 0.05 and abs(m[2]) < 0.05
        return o.get("upright", True)

    if hasattr(page, "filter"):
        page = page.filter(_reto)
    words = [w for w in page.extract_words(x_tolerance=1.5, y_tolerance=2, use_text_flow=False) if w["text"].strip()]
    if not words:
        return []
    H = page.height
    calha = _detectar_calha(words, page.width) if usar_colunas else None

    if calha is None:
        linhas = _agrupar_linhas(words)
    else:
        # linhas que atravessam a calha (títulos, cabeçalhos) viram "bandas" de largura total
        bandas = sorted((w["top"] - 1, w["bottom"] + 1) for w in words if w["x0"] < calha < w["x1"])
        fundidas = []
        for a, b in bandas:
            if fundidas and a <= fundidas[-1][1]:
                fundidas[-1][1] = max(fundidas[-1][1], b)
            else:
                fundidas.append([a, b])

        def cy(w):
            return (w["top"] + w["bottom"]) / 2

        def na_banda(w):
            return any(a <= cy(w) <= b for a, b in fundidas)

        fortes = [w for w in words if na_banda(w)]
        livres = [w for w in words if not na_banda(w)]
        esq = [w for w in livres if (w["x0"] + w["x1"]) / 2 < calha]
        dire = [w for w in livres if (w["x0"] + w["x1"]) / 2 >= calha]
        cortes = [-1.0] + [x for a, b in fundidas for x in (a, b)] + [H + 1.0]
        linhas = []
        for k in range(len(cortes) - 1):
            lo, hi = cortes[k], cortes[k + 1]
            if k % 2 == 0:  # segmento de 2 colunas: esquerda inteira, depois direita
                for grupo in (esq, dire):
                    linhas += _agrupar_linhas([w for w in grupo if lo <= cy(w) < hi])
            else:  # banda de largura total
                linhas += _agrupar_linhas([w for w in fortes if lo <= cy(w) <= hi])

    saida = []
    for L in linhas:
        zona = "topo" if L["top"] < 0.09 * H else ("base" if L["bottom"] > 0.93 * H else "corpo")
        saida.append({"t": L["texto"], "zona": zona})
    return saida


def ler_pdf(caminho, usar_colunas=True):
    paginas = []
    with pdfplumber.open(caminho) as pdf:
        for i, pg in enumerate(pdf.pages, 1):
            try:
                paginas.append(linhas_da_pagina(pg, usar_colunas))
            except Exception as e:  # página problemática não derruba o resto
                print(f"   ! página {i} de {os.path.basename(caminho)} falhou: {e}")
                paginas.append([])
    return paginas


# ---------------------------------------------------------------------------
# Limpeza de ruído (cabeçalho, rodapé, marca d'água)
# ---------------------------------------------------------------------------

RE_Q = re.compile(r"^\s*quest[ãa]o\s*[:\-–]?\s*(?:n[ºo°]\.?\s*)?0*(\d{1,3})\b[\s:.\-–)]*(.*)$", re.I)
RE_N = re.compile(r"^\s*0*(\d{1,3})\s*[\.\)\-–](?!\d)\s*(.*)$")
RE_N0 = re.compile(r"^\s*0*(\d{1,3})\s*$")
RE_ALT = re.compile(r"^\s*(?:\(\s*([A-Ea-e])\s*\)|([A-Ea-e])\s*[\)\.\-–])\s*(.*)$")

RE_RUIDO_LINHA = re.compile(r"^(?:p[áa]g(?:ina)?\.?\s*\d+(?:\s*(?:de|/)\s*\d+)?|\d{1,3}\s*/\s*\d{1,3}|[-–—\s\d]{1,6})$", re.I)
RE_RUIDO_TRECHO = [
    re.compile(p, re.I)
    for p in (
        r"www\.[\w\.\-/]+",
        r"https?://\S+",
        r"pci\s*concursos[\w\.\-]*",
        r"pcimark\S*",
        r"[A-Za-z0-9+/]{24,}={0,3}",
        r"^\s*CARGO\s*/?\s*(?:ESPECIALIDADE)?\s*:.*$",
    )
]


def _protegida(t):
    return bool(RE_Q.match(t) or RE_ALT.match(t))


def limpar_paginas(paginas):
    """Remove linhas de topo/base que se repetem entre páginas. Retorna (linhas_limpas, linhas_ruido)."""
    n = len(paginas)
    cont = Counter()
    for linhas in paginas:
        vistos = set()
        for L in linhas:
            if L["zona"] == "corpo" or _protegida(L["t"]):
                continue
            k = chave_repeticao(L["t"])
            if k and k not in vistos:
                vistos.add(k)
                cont[k] += 1
    limiar = max(2, math.ceil(0.3 * n)) if n > 1 else 10**9
    repetidos = {k for k, c in cont.items() if c >= limiar}

    limpas, ruido = [], []
    for linhas in paginas:
        for L in linhas:
            t = L["t"]
            if L["zona"] != "corpo":
                if chave_repeticao(t) in repetidos and not _protegida(t):
                    ruido.append(t)
                    continue
                if RE_RUIDO_LINHA.match(t):
                    ruido.append(t)
                    continue
            for rx in RE_RUIDO_TRECHO:
                t = rx.sub(" ", t)
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                limpas.append(t)
    return limpas, list(dict.fromkeys(ruido))


# ---------------------------------------------------------------------------
# Marcadores de questão
# ---------------------------------------------------------------------------


def achar_alternativas(lins):
    """Procura a última cadeia A,B,C,D(,E) nas linhas. Retorna [(idx, letra, resto)] ou None."""
    cands = []
    for i, t in enumerate(lins):
        m = RE_ALT.match(t)
        if m:
            cands.append((i, (m.group(1) or m.group(2)).upper(), m.group(3)))
    for minimo in (4, 3):
        for k in range(len(cands) - 1, -1, -1):
            if cands[k][1] != "A":
                continue
            cadeia, prox = [cands[k]], "B"
            for c in cands[k + 1:]:
                if c[1] == prox:
                    cadeia.append(c)
                    prox = chr(ord(prox) + 1)
                    if prox > "E":
                        break
            if len(cadeia) >= minimo:
                return cadeia
    return None


def _montar_cadeia(cands, lines, verificar, recuperar):
    aceitos, esperado = [], 1
    for i, (idx, num, resto) in enumerate(cands):
        ok = False
        if num == esperado:
            ok = True
            if verificar:
                fim = next((c[0] for c in cands[i + 1:] if c[1] == esperado + 1), len(lines))
                ok = achar_alternativas(lines[idx + 1:fim]) is not None
        elif recuperar and esperado < num <= esperado + 2:
            ok = any(c[1] == num + 1 for c in cands[i + 1:])
        if ok:
            aceitos.append((idx, num, resto))
            esperado = num + 1
    return aceitos


def localizar_marcadores(lines):
    q, n = [], []
    for i, t in enumerate(lines):
        m = RE_Q.match(t)
        if m:
            q.append((i, int(m.group(1)), m.group(2).strip()))
            continue
        m = RE_N.match(t)
        if m:
            n.append((i, int(m.group(1)), m.group(2).strip()))
            continue
        m = RE_N0.match(t)
        if m:
            n.append((i, int(m.group(1)), ""))
    cadeia_q = _montar_cadeia(q, lines, verificar=False, recuperar=True)
    cadeia_n = _montar_cadeia(n, lines, verificar=True, recuperar=False)
    if len(cadeia_q) >= 3 and len(cadeia_q) >= 0.6 * len(cadeia_n):
        return cadeia_q, "Questão N"
    if len(cadeia_n) >= 3:
        return cadeia_n, "N. / N)"
    return (cadeia_q if len(cadeia_q) >= len(cadeia_n) else cadeia_n), "indefinido"


# ---------------------------------------------------------------------------
# Blocos de questão: enunciado, alternativas, cauda (título de matéria / texto de apoio)
# ---------------------------------------------------------------------------

RE_ITEM = re.compile(r"^(?:I{1,3}|IV|VI{0,3}|IX|X)\s*[\.\)\-–]\s+\S|^\(\s*\)\s")
RE_APOIO = re.compile(
    r"^(?:TEXTO\b|LEIA\b|LEIAM\b|OBSERVE\b|INSTRUCAO|INSTRUCOES|AS QUESTOES? \d|AS QUESTOES DE|PARA RESPONDER|"
    r"RESPONDA|COM BASE NO|CONSIDERE O TEXTO|FIM DA PROVA|FIM DO CADERNO|RASCUNHO)"
)
BLACKLIST_MATERIA = (
    "INSTRUCOES", "ATENCAO", "RASCUNHO", "FOLHA", "CADERNO", "GABARITO", "TIPO ", "PROVA", "CARGO", "NOME",
    "INSCRICAO", "CONCURSO", "BOA ", "ASSINATURA", "FISCAL", "CANDIDATO", "TEXTO", "FIM ", "PAGINA", "EDITAL",
    "QUESTAO", "QUESTOES",
)


def unir(lins):
    out = ""
    for l in lins:
        l = l.strip()
        if not l:
            continue
        if not out:
            out = l
        elif RE_ITEM.match(l):
            out += "\n" + l
        elif out.endswith("-") and len(out) > 1 and out[-2].isalpha() and l[:1].islower():
            out = out[:-1] + l
        else:
            out += " " + l
    return re.sub(r"[ \t]+", " ", out).strip()


def eh_cabecalho(l):
    s = l.strip()
    if not (4 <= len(s) <= 100) or s != s.upper() or not re.search(r"[A-ZÀ-Ú]{3}", s):
        return False
    letras = sum(c.isalpha() for c in s)
    return letras >= 0.6 * len(s.replace(" ", ""))


def eh_apoio(l):
    return bool(RE_APOIO.match(norm(l)))


def materia_valida(h):
    n = norm(h)
    if len(n) < 5 or any(b in n for b in BLACKLIST_MATERIA) or COR_RE.search(n):
        return False
    return sum(c.isdigit() for c in n) <= 0.15 * len(n)


def alternativas_inline(lins):
    """Fallback: 'A) xx B) yy C) zz D) ww' na mesma linha."""
    texto = " ".join(lins)
    ms = [(m.start(), m.end(), m.group(1)) for m in re.finditer(r"(?<!\S)\(?([A-E])\)\s+", texto)]
    for k in range(len(ms) - 1, -1, -1):
        if ms[k][2] != "A":
            continue
        cadeia, prox = [ms[k]], "B"
        for m in ms[k + 1:]:
            if m[2] == prox:
                cadeia.append(m)
                prox = chr(ord(prox) + 1)
                if prox > "E":
                    break
        if len(cadeia) >= 4:
            enun = texto[:cadeia[0][0]]
            alts = []
            for j, (s, e, letra) in enumerate(cadeia):
                fim = cadeia[j + 1][0] if j + 1 < len(cadeia) else len(texto)
                alts.append((letra, [texto[e:fim].strip()]))
            return enun, alts
    return None


def parsear_bloco(lins):
    """Retorna (enunciado, [(letra, texto)], cauda_linhas)."""
    cadeia = achar_alternativas(lins)
    if cadeia:
        enun = unir(lins[:cadeia[0][0]])
        alts = []
        for j, (idx, letra, resto) in enumerate(cadeia):
            fim = cadeia[j + 1][0] if j + 1 < len(cadeia) else len(lins)
            alts.append((letra, ([resto] if resto else []) + lins[idx + 1:fim]))
        letra, corpo = alts[-1]
        corte = len(corpo)
        for k in range(1, len(corpo)):  # a 1ª linha é o texto da própria alternativa
            if eh_cabecalho(corpo[k]) or eh_apoio(corpo[k]):
                corte = k
                break
        cauda = corpo[corte:]
        alts[-1] = (letra, corpo[:corte])
        return enun, [(l, unir(c)) for l, c in alts], cauda
    inl = alternativas_inline(lins)
    if inl:
        enun, alts = inl
        return unir([enun]), [(l, unir(c)) for l, c in alts], []
    return unir(lins), [], []


def processar_cauda(cauda):
    """Separa título(s) de matéria (linhas em caixa-alta) do texto de apoio que vem a seguir."""
    i, cab = 0, []
    while i < len(cauda) and eh_cabecalho(cauda[i]) and not eh_apoio(cauda[i]):
        cab.append(cauda[i])
        i += 1
    materia = " ".join(cab).strip() if cab and materia_valida(" ".join(cab)) else None
    apoio_lins = cauda[i:]
    apoio, fim = None, None
    if apoio_lins and eh_apoio(apoio_lins[0]) and not re.match(r"^(FIM|RASCUNHO)", norm(apoio_lins[0])):
        apoio = unir(apoio_lins)
        m = re.search(r"QUEST(?:OES|AO)\s*(?:DE\s*)?0*(\d{1,3})\s*(?:A|E|AO|ATE|-)\s*0*(\d{1,3})", norm(apoio))
        if m:
            fim = int(m.group(2))
    return materia, apoio, fim


RE_FAIXA = re.compile(
    r"^\s*(?:[IVX]+\s*[-–.)]\s*|PARTE\s+\w+\s*[-–:]\s*)?(?P<t>[^\d\(\):]{4,80}?)\s*[\(:\-–]*\s*"
    r"(?:quest[õo]es|quest[ãa]o)?\s*(?:de\s+|n[ºo°.]*\s*)?0*(?P<a>\d{1,3})\s*(?:a|à|ao|até|-|–)\s*0*(?P<b>\d{1,3})\s*\)?\s*$",
    re.I,
)


def mapa_materias(preambulo):
    """Procura no preâmbulo linhas do tipo 'Língua Portuguesa – Questões 1 a 10'."""
    faixas = []
    i = 0
    while i < len(preambulo):
        l, usou = preambulo[i], 1
        m = RE_FAIXA.match(l)
        if not m and i + 1 < len(preambulo):  # a linha pode ter quebrado no meio ("Questões 7" / "a 12")
            m = RE_FAIXA.match(l + " " + preambulo[i + 1])
            usou = 2 if m else 1
        i += usou
        if not m:
            continue
        t = m.group("t").strip(" -–:()")
        a, b = int(m.group("a")), int(m.group("b"))
        if a < 1 or b < a or b > 300 or not materia_valida(t):
            continue
        faixas.append((a, b, re.sub(r"\s+", " ", t).upper()))
    faixas.sort()
    for (a1, b1, _), (a2, b2, _) in zip(faixas, faixas[1:]):
        if a2 <= b1:  # sobreposição -> não confiável
            return {}
    mapa = {}
    for a, b, t in faixas:
        for q in range(a, b + 1):
            mapa[q] = t
    return mapa


def materia_do_preambulo(preambulo):
    for l in reversed(preambulo[-25:]):
        if eh_cabecalho(l) and materia_valida(l):
            return l.strip()
    return None


# ---------------------------------------------------------------------------
# Identidade da prova (para achar o gabarito certo)
# ---------------------------------------------------------------------------

STOP = {
    "PROVA", "OBJETIVA", "CADERNO", "CARGO", "CARGOS", "CONCURSO", "PUBLICO", "PARA", "COM", "DOS", "DAS", "NOS",
    "QUESTOES", "QUESTAO", "TIPO", "VERSAO", "MODELO", "WWW", "BRASIL", "EDITAL", "DE", "DA", "DO", "AS", "OS",
    "GABARITO", "PRELIMINAR", "DEFINITIVO", "OFICIAL", "ESPECIALIDADE", "AREA", "NIVEL", "CODIGO", "PAGINA",
}


def tokens_texto(texto):
    n = norm(texto)
    toks = {t for t in re.findall(r"[A-Z0-9]{3,}", n) if t not in STOP and not COR_RE.fullmatch(t)}
    for m in re.finditer(r"\b([A-Z]{3,})\s+(\d{1,2})\b", n):  # "DEFENSORIA 2"
        if m.group(1) not in STOP:
            toks.add(f"{m.group(1)}_{m.group(2)}")
    return toks


RE_CHAVE_IDENT = re.compile(r"\b(CARGO|ESPECIALIDADE|AREA|EMPREGO|FUNCAO|CARREIRA|TIPO|CADERNO|VERSAO|MODELO|NIVEL|COR)\b")


def identificar_prova(paginas_brutas, ruido):
    linhas, margens = [], []
    for pg in paginas_brutas[:2]:
        linhas += [L["t"] for L in pg]
        margens += [L["t"] for L in pg if L["zona"] != "corpo"]
    chave = [l for l in linhas[:120] if RE_CHAVE_IDENT.search(norm(l)) or COR_RE.search(norm(l))][:40]
    ident_linhas = list(dict.fromkeys(ruido + margens + chave))
    txt = norm(" | ".join(ident_linhas))
    tipo = TIPO_RE.findall(txt)
    cor = [cor_canon(c) for c in COR_RE.findall(txt)]
    cargo = ""
    for l in linhas[:120]:
        m = re.search(r"(?:CARGO|ESPECIALIDADE|EMPREGO|FUN[ÇC][ÃA]O)\s*/?\s*(?:ESPECIALIDADE)?\s*[:\-–]\s*(.+)$", l, re.I)
        if m:
            cargo = m.group(1).strip()
            break
    return {
        "texto": " | ".join(ident_linhas),
        "tokens": tokens_texto(" ".join(ident_linhas)),
        "tipo": Counter(tipo).most_common(1)[0][0] if tipo else "",
        "cor": Counter(cor).most_common(1)[0][0] if cor else "",
        "cargo": cargo,
    }


# ---------------------------------------------------------------------------
# Gabarito consolidado
# ---------------------------------------------------------------------------

RE_PAR = re.compile(r"(?<![\d.,/])0*(\d{1,3})\s*[\-–:\.\)]?\s*(ANULAD[AO]|[A-E*])(?![A-Za-zÀ-ÿ0-9])")
RE_COLS = re.compile(r"^\s*0*(\d{1,3})\s+((?:(?:[A-E*X]|ANULAD[AO])(?:\s+|$))+)$")
RE_PALAVRAS_SOLTAS = re.compile(r"\b(?:quest(?:ões|oes|ão|ao)|n[º°]|item|resposta|gabarito)\b", re.I)


def _val(r):
    r = r.upper()
    return "ANULADA" if r.startswith(("ANUL", "*", "X")) else r


def pares_da_linha(l):
    ms = list(RE_PAR.finditer(l))
    if not ms:
        return None
    resto = RE_PALAVRAS_SOLTAS.sub(" ", RE_PAR.sub(" ", l))
    a_resto = len(re.sub(r"[^0-9A-Za-zÀ-ÿ]", "", resto))
    a_total = len(re.sub(r"[^0-9A-Za-zÀ-ÿ]", "", l))
    if a_resto > max(3, 0.25 * a_total):
        return None
    return [(int(m.group(1)), _val(m.group(2))) for m in ms]


def linha_colunas(l):
    m = RE_COLS.match(l.strip())
    if not m:
        return None
    toks = m.group(2).split()
    return (int(m.group(1)), [_val(t) for t in toks]) if len(toks) >= 2 else None


def linha_numeros(l):
    toks = l.split()
    return [int(t) for t in toks] if len(toks) >= 5 and all(t.isdigit() for t in toks) else None


def linha_letras(l):
    toks = l.split()
    return [_val(t) for t in toks] if len(toks) >= 5 and all(re.fullmatch(r"[A-E*X]|ANULAD[AO]", t) for t in toks) else None


def dividir_gabarito(linhas):
    """Divide em blocos a cada reinício da numeração em 1. Os títulos acima da 1ª resposta viram o cabeçalho do bloco."""
    blocos, atual, buf, maxnum = [], None, [], 0

    def classe(l):
        p = pares_da_linha(l)
        if p:
            return "resp", p[0][0], max(n for n, _ in p)
        c = linha_colunas(l)
        if c:
            return "resp", c[0], c[0]
        n = linha_numeros(l)
        if n:
            return "num", n[0], max(n)
        if linha_letras(l):
            return "let", None, None
        return "txt", None, None

    for l in linhas:
        tipo, n1, nmax = classe(l)
        if tipo == "txt":
            buf.append(l)
            continue
        novo = atual is None or (tipo in ("resp", "num") and n1 == 1 and maxnum >= 3)
        if novo:
            atual = {"cab": buf[-10:], "itens": [], "txt": list(buf[-10:])}
            blocos.append(atual)
            maxnum = 0
        else:
            atual["itens"] += [("txt", t) for t in buf]
            atual["txt"] += buf
        buf = []
        atual["itens"].append((tipo, l))
        if tipo in ("resp", "num") and nmax:
            maxnum = max(maxnum, nmax)
    if atual is not None and buf:
        atual["itens"] += [("txt", t) for t in buf]
        atual["txt"] += buf
    return blocos


def _rotulos_colunas(txts):
    tipos, cores = [], []
    for l in txts:
        n = norm(l)
        t = re.findall(r"\b(?:TIPO|VERSAO|MODELO|CADERNO|PROVA)\s*[:\-]?\s*(\d{1,2}|[A-D])\b", n)
        c = [cor_canon(x) for x in COR_RE.findall(n)]
        if len(t) >= 2 and len(t) > len(tipos):
            tipos = t
        if len(c) >= 2 and len(c) > len(cores):
            cores = c
    return tipos, cores


def respostas_do_bloco(bloco, ident):
    """Devolve lista de (estrategia, dict_respostas, ambiguo)."""
    cands = []
    tipos, cores = _rotulos_colunas(bloco["txt"])
    ncol = len(tipos) or len(cores)
    # S1: tabela com uma coluna por tipo/cor
    if ncol >= 2:
        idx = None
        if ident["tipo"] and ident["tipo"] in tipos:
            idx = tipos.index(ident["tipo"])
        elif ident["cor"] and ident["cor"] in cores:
            idx = cores.index(ident["cor"])
        if idx is not None:
            ans = {}
            for tipo, l in bloco["itens"]:
                c = linha_colunas(l) if tipo == "resp" else None
                if c and len(c[1]) == ncol:
                    ans.setdefault(c[0], c[1][idx])
            if ans:
                cands.append(("tabela por tipo/cor", ans, False))
    # S2: linha de números seguida de linha de letras
    ans, itens = {}, bloco["itens"]
    for i in range(len(itens) - 1):
        if itens[i][0] == "num" and itens[i + 1][0] == "let":
            ns, ls = linha_numeros(itens[i][1]), linha_letras(itens[i + 1][1])
            if ns and ls and len(ns) == len(ls):
                for n, r in zip(ns, ls):
                    ans.setdefault(n, r)
    if ans:
        cands.append(("linha de números + linha de letras", ans, False))
    # S3: pares "01 A"
    ans = {}
    for tipo, l in itens:
        if tipo == "resp":
            for n, r in (pares_da_linha(l) or []):
                ans.setdefault(n, r)
    if ans:
        cands.append(("lista de pares", ans, ncol >= 2))
    return cands


def escolher_gabarito(blocos, ident, n_questoes):
    """Pontua cada bloco contra a identidade da prova. Retorna (respostas, descrição, aviso)."""
    if not blocos:
        return {}, "nenhum bloco de respostas encontrado no PDF", True
    toks_blocos = [tokens_texto(" ".join(b["txt"])) for b in blocos]
    df = Counter(t for ts in toks_blocos for t in ts)
    nb = len(blocos)

    def idf(t):
        return math.log((nb + 1) / (df.get(t, 0) + 0.5))

    # só contam tokens da identidade que aparecem em algum bloco (o resto é ruído do cabeçalho da prova)
    relevantes = {t for t in ident["tokens"] if df.get(t, 0) > 0}
    total_idf = sum(idf(t) for t in relevantes) or 1.0

    resultados = []
    for b, toks in zip(blocos, toks_blocos):
        txt = norm(" ".join(b["txt"]))
        tipos_b = set(TIPO_RE.findall(txt)) | set(re.findall(r"\bPROVA\s*(\d{1,2})\b", txt))
        cores_b = {cor_canon(c) for c in COR_RE.findall(txt)}
        acertos = sum(idf(t) for t in relevantes if t in toks)
        sobras = sum(idf(t) for t in toks if t not in ident["tokens"])  # palavras do bloco que a prova não tem
        s_id = 4 * (acertos - 0.7 * sobras) / total_idf
        s_tc = 0.0
        if ident["tipo"]:
            s_tc += 2.5 if ident["tipo"] in tipos_b else (-2.5 if tipos_b else 0)
        if ident["cor"]:
            s_tc += 2.5 if ident["cor"] in cores_b else (-2.5 if cores_b else 0)
        melhor = None
        cands = respostas_do_bloco(b, ident)
        ordem = {"tabela por tipo/cor": 3, "linha de números + linha de letras": 2, "lista de pares": 1}
        for estrat, ans, amb in cands:
            if n_questoes:
                ans = {k: v for k, v in ans.items() if 1 <= k <= n_questoes}
            if not ans:
                continue
            cobertura = (min(len(ans), n_questoes) / max(len(ans), n_questoes)) if n_questoes else 0.5
            chave = (round(cobertura, 3), ordem[estrat])
            if melhor is None or chave > melhor[0]:
                melhor = (chave, estrat, ans, amb, cobertura)
        if melhor is None:
            resultados.append((-99, 0, "", {}, False, b))
            continue
        _, estrat, ans, amb, cobertura = melhor
        score = s_id + s_tc + 4 * cobertura
        resultados.append((score, cobertura, estrat, ans, amb, b))

    resultados_validos = [r for r in resultados if r[3]]
    if not resultados_validos:
        return {}, "nenhum bloco do gabarito tinha respostas legíveis", True
    # em caso de empate (ex.: preliminar x definitivo), vale o último do PDF
    melhor = max(enumerate(resultados_validos), key=lambda kv: (round(kv[1][0], 6), kv[0]))[1]
    segundo = sorted((r[0] for r in resultados_validos), reverse=True)
    margem = segundo[0] - (segundo[1] if len(segundo) > 1 else -9)
    score, cobertura, estrat, ans, amb, bloco = melhor
    cab = " / ".join(bloco["cab"][-3:]) if bloco["cab"] else "(sem título)"
    desc = (f"bloco '{cab[:90]}' | {len(ans)} respostas ({estrat}) | pontuação {score:.1f}, margem {margem:.1f}")
    aviso = amb or margem < 1.0 or (n_questoes and cobertura < 0.9)
    return ans, desc, bool(aviso)


# ---------------------------------------------------------------------------
# Processamento de uma prova
# ---------------------------------------------------------------------------


def processar_prova(caminho_prova, caminho_gab, rotulo, debug_dir, cache_gab):
    paginas = ler_pdf(caminho_prova, usar_colunas=True)
    linhas, ruido = limpar_paginas(paginas)
    ident = identificar_prova(paginas, ruido)

    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
        with open(os.path.join(debug_dir, f"{rotulo}_texto_limpo.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(linhas))
        with open(os.path.join(debug_dir, f"{rotulo}_ruido_removido.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(ruido))

    cadeia, formato = localizar_marcadores(linhas)
    info = {"rotulo": rotulo, "arquivo": os.path.basename(caminho_prova), "formato": formato,
            "cargo": ident["cargo"], "tipo": ident["tipo"], "cor": ident["cor"], "avisos": []}
    if len(cadeia) < 3:
        info["avisos"].append("não encontrei marcadores de questão (veja debug/)")
        if debug_dir is None:
            d = "debug"
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{rotulo}_texto_limpo.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(linhas))
        return info, []

    n_q = len(cadeia)
    # gabarito
    gabarito, desc_gab = {}, "sem gabarito"
    if caminho_gab:
        if caminho_gab not in cache_gab:
            pg = ler_pdf(caminho_gab, usar_colunas=False)
            cache_gab[caminho_gab] = dividir_gabarito([L["t"] for p in pg for L in p])
            if debug_dir:
                with open(os.path.join(debug_dir, os.path.basename(caminho_gab) + ".txt"), "w", encoding="utf-8") as f:
                    f.write("\n".join(L["t"] for p in pg for L in p))
        gabarito, desc_gab, aviso = escolher_gabarito(cache_gab[caminho_gab], ident, cadeia[-1][1])
        if aviso:
            info["avisos"].append("gabarito incerto: " + desc_gab)
    info["gabarito"] = desc_gab

    primeiro = cadeia[0][0]
    preambulo = linhas[:primeiro]
    mapa = mapa_materias(preambulo)
    materia_atual = "Conhecimentos Gerais"
    pend_materia, pend_apoio, pend_fim = None, None, None
    # título de matéria e texto de apoio que vêm logo antes da 1ª questão
    for j in range(len(preambulo) - 1, max(-1, len(preambulo) - 40), -1):
        if eh_cabecalho(preambulo[j]) and materia_valida(preambulo[j]):
            pend_materia, pend_apoio, pend_fim = processar_cauda(preambulo[j:])
            break
    apoio_atual, apoio_fim = None, 0

    questoes = []
    for k, (idx, num, resto) in enumerate(cadeia):
        fim_idx = cadeia[k + 1][0] if k + 1 < len(cadeia) else len(linhas)
        bloco = ([resto] if resto else []) + linhas[idx + 1:fim_idx]

        if pend_materia:
            materia_atual, apoio_atual = pend_materia, None
            pend_materia = None
        if pend_apoio:
            apoio_atual, apoio_fim = pend_apoio, (pend_fim or num + 8)
            pend_apoio = pend_fim = None
        if mapa.get(num) and mapa[num] != materia_atual:
            materia_atual, apoio_atual = mapa[num], None
        if apoio_atual and num > apoio_fim:
            apoio_atual = None

        enun, alts, cauda = parsear_bloco(bloco)
        mat, apoio, fim = processar_cauda(cauda)
        pend_materia, pend_apoio, pend_fim = mat, apoio, fim

        resp = gabarito.get(num, "")
        alternativas = [{"letra": l, "texto": t} for l, t in alts]
        if not alternativas and resp in ("C", "E"):  # estilo Certo/Errado
            alternativas = [{"letra": "C", "texto": "Certo"}, {"letra": "E", "texto": "Errado"}]

        avisos = []
        if not alternativas:
            avisos.append("sem alternativas")
        elif len(alternativas) < 4 and not (len(alternativas) == 2 and resp in ("C", "E")):
            avisos.append(f"só {len(alternativas)} alternativas")
        if not enun or len(enun) < 15:
            avisos.append("enunciado vazio/curto")
        if len(enun) > 2500:
            avisos.append("enunciado muito longo")
        if re.search(r"(?<!\S)\(?[BC]\)\s", enun):
            avisos.append("alternativas misturadas no enunciado")
        if not resp:
            avisos.append("sem gabarito")

        questoes.append({
            "uid": f"{rotulo}-q{num}",
            "numero": num,
            "prova": rotulo,
            "materia": materia_atual,
            "apoio": apoio_atual or "",
            "enunciado": enun,
            "alternativas": alternativas,
            "resposta_correta": resp,
            "avisos": avisos,
        })

    info["n_questoes"] = n_q
    info["n_com_gabarito"] = sum(1 for q in questoes if q["resposta_correta"])
    return info, questoes


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------


def numero_do_arquivo(caminho):
    m = re.search(r"(\d+)", os.path.basename(caminho))
    return int(m.group(1)) if m else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pasta", default=".")
    ap.add_argument("--saida", default="questoes.json")
    ap.add_argument("--debug", action="store_true", help="grava o texto limpo/ruído de cada PDF em debug/")
    args = ap.parse_args()

    pasta = args.pasta
    provas = sorted(glob.glob(os.path.join(pasta, "prova_*.pdf")), key=numero_do_arquivo)
    if not provas:
        print(f"Nenhum prova_*.pdf encontrado em {os.path.abspath(pasta)}")
        sys.exit(1)

    debug_dir = os.path.join(pasta, "debug") if args.debug else None
    cache_gab = {}
    todas, infos = [], []
    print("Processando provas...\n")
    for caminho in provas:
        i = numero_do_arquivo(caminho)
        gab = os.path.join(pasta, f"gabarito_{i}.pdf")
        if not os.path.exists(gab):
            alt = os.path.join(pasta, "gabarito.pdf")
            gab = alt if os.path.exists(alt) else None
        rotulo = f"Prova {i}"
        print(f"== {os.path.basename(caminho)}  +  {os.path.basename(gab) if gab else '(sem gabarito)'}")
        try:
            info, qs = processar_prova(caminho, gab, rotulo, debug_dir, cache_gab)
        except Exception as e:
            print(f"   ERRO: {e}\n")
            continue
        print(f"   identidade: cargo='{info['cargo'][:60]}' tipo='{info['tipo']}' cor='{info['cor']}' | formato: {info['formato']}")
        if info.get("gabarito"):
            print(f"   gabarito: {info['gabarito']}")
        print(f"   {len(qs)} questões, {info.get('n_com_gabarito', 0)} com gabarito")
        for a in info["avisos"]:
            print(f"   ! {a}")
        problemas = {}
        for q in qs:
            for a in q["avisos"]:
                problemas.setdefault(a, []).append(q["numero"])
        for a, nums in problemas.items():
            lista = ", ".join(map(str, nums[:12])) + (" ..." if len(nums) > 12 else "")
            print(f"   ! {a}: {len(nums)} questão(ões) -> {lista}")
        print()
        todas += qs
        infos.append({k: v for k, v in info.items() if k != "avisos"})

    # unifica nomes de matéria que só diferem por acento/caixa
    canon = {}
    for q in todas:
        q["materia"] = canon.setdefault(norm(q["materia"]), q["materia"])

    dados = {"concurso": "Simulado Geral - Concursos Públicos", "provas": infos, "questoes": todas}
    with open(os.path.join(pasta, args.saida), "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

    sem_g = sum(1 for q in todas if not q["resposta_correta"])
    print(f"Pronto! {len(todas)} questões salvas em '{args.saida}' ({sem_g} sem gabarito).")
    if any(q["avisos"] for q in todas):
        print("Se algo ficou estranho, rode com --debug e olhe a pasta debug/ (texto limpo de cada PDF).")


if __name__ == "__main__":
    main()

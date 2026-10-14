import json
import os
import re
from pypdf import PdfReader


def extrair_texto_pdf(caminho_pdf):
  leitor = PdfReader(caminho_pdf)
  texto = ""
  for pagina in leitor.pages:
    txt = pagina.extract_text()
    if txt:
      texto += txt + "\n"
  return texto


def parsear_gabarito(caminho_gabarito_pdf):
  texto = extrair_texto_pdf(caminho_gabarito_pdf)
  gabarito = {}
  padroes = re.findall(r"(\d+)\s*[\-|\|\.]\s*([A-E])", texto)
  for q_num, q_resp in padroes:
    gabarito[int(q_num)] = q_resp.upper()
  return gabarito


def processar_prova(caminho_prova_pdf, caminho_gabarito_pdf=None):
  texto = extrair_texto_pdf(caminho_prova_pdf)
  gabarito = (
      parsear_gabarito(caminho_gabarito_pdf) if caminho_gabarito_pdf else {}
  )

  termos_materias = [
      "LÍNGUA PORTUGUESA",
      "LEGISLAÇÃO",
      "CONHECIMENTOS GERAIS",
      "INFORMÁTICA",
      "DIREITO CONSTITUCIONAL",
      "DIREITO ADMINISTRATIVO",
      "CONHECIMENTOS ESPECÍFICOS",
      "MATEMÁTICA",
      "RACIOCÍNIO LÓGICO",
  ]

  brutos_questoes = re.split(r"(?:Questão|QUESTÃO)\s*(\d+)", texto)
  questoes_extraidas = []

  for i in range(1, len(brutos_questoes), 2):
    q_num = int(brutos_questoes[i])
    q_bloco = brutos_questoes[i + 1]

    materia_atual = "Conhecimentos Gerais / Outros"
    texto_antecedente = brutos_questoes[i - 1]
    for m in termos_materias:
      if m in texto_antecedente.upper() or m in q_bloco.upper():
        materia_atual = m

    partes_alt = re.split(r"\n\s*([A-E])\)", q_bloco)
    enunciado = partes_alt[0].strip()
    alternativas = []

    if len(partes_alt) > 1:
      for j in range(1, len(partes_alt) - 1, 2):
        letra = partes_alt[j]
        texto_alt = partes_alt[j + 1].strip().replace("\n", " ")
        alternativas.append(f"{letra}) {texto_alt}")

    resp_correta = gabarito.get(q_num, "A")

    questoes_extraidas.append({
        "id": q_num,
        "materia": materia_atual,
        "enunciado": enunciado.replace("\n", " "),
        "alternativas": alternativas,
        "resposta_correta": resp_correta,
    })

  return questoes_extraidas


if __name__ == "__main__":
  print("Processando todas as provas e gabaritos automaticamente...")

  todas_as_questoes = []
  i = 1

  # Varre automaticamente a pasta procurando por prova_1.pdf, prova_2.pdf...
  while True:
    prova_path = f"prova_{i}.pdf"
    gabarito_path = f"gabarito_{i}.pdf"

    if not os.path.exists(prova_path):
      break  # Para quando acabar os arquivos numerados

    print(f"Processando {prova_path} e {gabarito_path}...")

    # Se o gabarito correspondente existir, passa ele; senão, processa só a prova
    gab = gabarito_path if os.path.exists(gabarito_path) else None
    questoes_prova = processar_prova(prova_path, gab)

    # Adiciona a identificação da prova em cada questão
    for q in questoes_prova:
      q["prova"] = f"Prova {i}"
      todas_as_questoes.append(q)

    i += 1

  dados_final = {
      "concurso": "Simulado Geral - Concursos Públicos",
      "questoes": todas_as_questoes,
  }

  with open("questoes.json", "w", encoding="utf-8") as f:
    json.dump(dados_final, f, ensure_ascii=False, indent=4)

  print(
      f"Sucesso! {len(todas_as_questoes)} questões extraídas e salvas em"
      " 'questoes.json'."
  )
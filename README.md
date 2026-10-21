# REAI Scroll Sequence Engine

Uma ferramenta local para transformar um vídeo numa sequência de imagens preparada para animação controlada pelo scroll. Desenvolvido por **Nulled_**.

## Requisitos

- Python 3.10 ou superior
- FFmpeg e FFprobe instalados e disponíveis no `PATH`
- `tqdm` (opcional; melhora a barra de progresso no terminal)

Instalar dependência opcional:

```powershell
python -m pip install tqdm
```

## Começar no Windows

1. Coloca o vídeo numa pasta `Videos/` (ou usa um caminho completo).
2. Abre PowerShell na raiz do projeto.
3. Executa:

```powershell
python cli/process_video.py --input "Videos/seu_video.mp4" --output "assets/frames" --fps 30 --quality 70
```

O processamento gera um `manifest.json` e versões desktop/mobile dentro de `assets/frames/`.

Para verificar os parâmetros disponíveis:

```powershell
python cli/process_video.py --help
```

Para apenas inspecionar o comando que seria executado:

```powershell
python cli/process_video.py --input "Videos/seu_video.mp4" --dry-run
```

## Pré-visualizar o site

Depois de gerar os frames, inicia um servidor HTTP na raiz do projeto:

```powershell
python -m http.server 8000
```

Abre `http://localhost:8000` no browser. O acesso via HTTP é necessário para o carregamento do manifesto.

## Estrutura

```text
reai-scroll-engine/
├── index.html
├── js/
│   └── scroll-engine.js
├── cli/
│   └── process_video.py
├── assets/
│   └── frames/       # gerado pela CLI; não incluído no ZIP
├── Videos/           # coloca aqui os teus vídeos de origem
├── run-reai.bat
└── README.md
```

## Notas importantes

- Os frames e vídeos de origem não estão incluídos neste pacote, para manter o ZIP pequeno.
- A interpolação de movimento é opcional e pode aumentar bastante o tempo de processamento.
- Os valores padrão da CLI favorecem um equilíbrio entre qualidade e velocidade; aumenta `--quality` ou a altura das variantes se precisares de maior fidelidade.
- O motor não inventa frames coloridos quando um ficheiro falha: regista o erro e mantém o frame válido mais próximo até a sequência avançar.
- O link de GitHub no site aponta para `https://github.com/Hugo-JC`. Altera-o no `index.html` caso o repositório público do projeto tenha outro URL.

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
3. Usa o lançador simples no PowerShell ou na Linha de Comandos:

```powershell
.\reai.bat processar "Videos\seu_video.mp4"
```

O processamento gera um `manifest.json` e versões desktop/mobile dentro de
`assets/frames/`. Podes indicar FPS e qualidade opcionalmente:

```powershell
.\reai.bat processar "Videos\seu_video.mp4" 30 70
```

Para abrir um menu interativo, executa `.\reai.bat` sem argumentos. Os comandos
simples disponíveis são:

- `.\reai.bat processar "Videos\seu_video.mp4" [fps] [qualidade]` — processa o vídeo.
- `.\reai.bat simular "Videos\seu_video.mp4"` — mostra o comando FFmpeg sem o executar.
- `.\reai.bat prever` — gera e abre a pré-visualização offline.
- `.\reai.bat limpar` — apaga os frames e ficheiros gerados.
- `.\reai.bat site` — inicia o site local em `http://localhost:8000`.
- `.\reai.bat opcoes` — mostra todas as opções avançadas da CLI.

O atalho `.\run-reai.bat "Videos\seu_video.mp4"` continua disponível para
processar um vídeo e iniciar o site local numa só execução.

Também podes usar diretamente o processador com opções avançadas, por exemplo:

```powershell
python cli/process_video.py --input "Videos/seu_video.mp4" --fps 30 --quality 70 --format webp
```

Para verificar os parâmetros disponíveis:

```powershell
python cli/process_video.py --help
```

Para gerar uma pré-visualização interativa offline — com seletor desktop/mobile,
controlo de frames e suporte a atlases — a partir dos frames existentes:

```powershell
.\reai.bat prever
```

O comando cria `assets/frames/preview.html`. Abre esse ficheiro no browser; não
precisas de iniciar um servidor. Para usar outro diretório, acrescenta
`--output "caminho/para/frames"`.

Para apagar os frames gerados em `desktop` e `mobile`, o manifesto e a
pré-visualização:

```powershell
.\reai.bat limpar
```

Por omissão, a limpeza usa `assets/frames`. Para indicar outro diretório:

```powershell
python cli/process_video.py --clean-frames --output "caminho/para/frames"
```

Para apenas inspecionar o comando que seria executado:

```powershell
.\reai.bat simular "Videos\seu_video.mp4"
```

### Opções de processamento

O comando de processamento aceita `-i/--input`, `-o/--output`, `--fps`,
`--max-frames`, `--format`, `--quality`, `--effort`, `--desktop-height`,
`--mobile-height`, `--only`, `--interp`, `--no-ai`, `--hwaccel`, `--start`,
`--end`, `--atlas`, `--web-prefix`, `--stall-timeout` e `--dry-run`.
Os comandos adicionais são `--preview` e `--clean-frames`. Executa
`python cli/process_video.py --help` para descrições e valores predefinidos.

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
├── reai.bat           # comandos simples e menu para Windows
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

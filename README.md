# DeckInstall

Instalador e gerenciador simples de jogos Não-Steam para **Steam Deck / SteamOS** (KDE Plasma + Qt6).

## Funcionalidades
- **Extrair / Instalar**: descompacta `.zip`, `.rar`, `.7z`, `.tar.*` direto para `~/Games/<Jogo>` (evitando pastas duplas) ou executa `setup.exe` via Proton em prefixo temporário.
- **Detecção Inteligente**: varre `~/Games`, `~/Downloads`, `~/Syncthing` e cartão SD (`/run/media/*`), ignora binários auxiliares e prioriza o executável real do jogo.
- **Artes Oficiais da Steam**: identifica `steam_appid.txt` / `steam_emu.ini` ou busca na Steam Store API e baixa capa vertical, horizontal, hero e logo da CDN oficial (`shared.fastly.steamstatic.com`).
- **Adição Segura ao Steam**: fecha o Steam com segurança, faz backup de `shortcuts.vdf` e `config.vdf`, grava o atalho com `proton_experimental` ativo e reabre o Steam.
- **Temas**: modos **Automático**, **Escuro** e **Claro**.

## Desenvolvimento local
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Build AppImage (GitHub Actions)
Crie uma tag semântica (`v0.1.0`, etc.) e faça push para gerar e publicar o `DeckInstall-<versão>-x86_64.AppImage` automaticamente.

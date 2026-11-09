# Changelog

Todas as mudanças relevantes deste projeto são documentadas aqui.
O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o projeto usa [SemVer](https://semver.org/lang/pt-BR/).

## [1.0.0] - 2026-11-09

### Adicionado
- Detecção automática da instalação do Thunderbird, dos perfis, das contas e da árvore de pastas (mbox e maildir).
- Exportação a partir dos arquivos locais do Thunderbird ou direto do servidor IMAP.
- Formatos MBOX e EML, com `manifest.json` descrevendo a exportação.
- Importação para qualquer servidor IMAP, preservando datas e flags (lida, respondida, com estrela).
- Importação para as Pastas Locais do Thunderbird.
- Mapeamento automático de Entrada/Enviados/Rascunhos/Lixeira/Spam/Arquivo e remoção do prefixo `[Gmail]`.
- Detecção de duplicatas por `Message-ID`, reconexão automática, cancelamento e log em arquivo.
- Interface gráfica em português e executável único para Windows.

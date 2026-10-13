# 💅 Polish Watcher

Monitor de **produtos novos** e **restock** para lojas online de esmaltes indie, com alertas no **Telegram**. Roda de graça no **GitHub Actions**, a cada 15 minutos.

O monitor lê o catálogo **inteiro** de cada loja e avisa quando:

| Alerta | Quando dispara | Padrão |
|---|---|---|
| 🆕 NOVIDADE | apareceu um produto que não existia | ligado |
| 🚨 RESTOCK | produto (ou variante) passou de esgotado para disponível | ligado |
| 📦 REPOSIÇÃO | chegaram mais unidades num produto que **não** tinha esgotado (ex.: 3 → 30), a partir do mínimo configurado | ligado (5+ unidades) |
| ⚡ Esgotou | um produto anunciado esgotou (até 48h depois): o bot **responde ao alerta original** dizendo em quanto tempo | desligado |
| 🔓 / 🔒 Loja aberta / fechada | a loja reabriu ou fechou com senha (lançamento, reforma) | ligado |
| ⏳ ÚLTIMAS UNIDADES | o estoque caiu para o limite que você escolheu (ex.: 3) | desligado |
| 🏷️ PREÇO MUDOU | o preço do produto mudou | desligado |
| ⚠️ Erro | a loja falhou várias rodadas seguidas (site fora do ar, mudança no site) | ligado |

Cada alerta chega com a **foto do produto**, nome, loja, preço e estoque, e tem:

- **botões** "🛒 Comprar" e "🏪 Ver a loja";
- **hashtags** da loja e do tipo (ex.: `#Whatcha #restock`). Tocando numa hashtag, você vê todos os alertas daquela loja ou daquele tipo;
- **selo de urgência** "🔥 Últimas unidades. Corre!" quando o estoque está baixo (limite em `urgencia_estoque`).

A quantidade em estoque **não aparece** nos alertas (`telegram: mostrar_estoque: false`); o monitor continua usando o número internamente para detectar restock, reposição e urgência.

Mudança de preço, loja fechada, avisos de erro e "esgotou" chegam **sem som**. Novidade, restock e loja aberta tocam normalmente. Se aparecerem muitos alertas de uma vez (ex.: lançamento de coleção), eles chegam agrupados em álbuns de até 10 fotos com a lista numerada.

---

## ➕ Como adicionar uma loja

Tudo fica no arquivo **`sites.yaml`**. Para adicionar uma loja, basta incluir um item no fim da lista `lojas`:

```yaml
lojas:
  # ...lojas que já existem...

  - nome: Nova Loja
    url: https://www.novaloja.com.br/
```

Pronto. Na próxima rodada o monitor:

1. descobre sozinho a plataforma (Nuvemshop ou Shopify);
2. registra o catálogo atual como **linha de base**, **sem enviar alertas**;
3. a partir da rodada seguinte, avisa de tudo o que mudar.

> Dá para editar o `sites.yaml` direto pelo site do GitHub: abra o arquivo, clique no lápis ✏️, edite e clique em **Commit changes**.

### Todas as opções de uma loja

```yaml
  - nome: Minha Loja                       # nome que aparece nos alertas
    url: https://www.minhaloja.com.br/     # endereço da loja (obrigatório)
    plataforma: auto                       # auto | nuvemshop | shopify
    alertas:
      novos: true                          # produto novo
      restock: true                        # voltou ao estoque
      reposicao: 5                         # false, ou avisa quando chegarem 5+ unidades sem ter esgotado
      estoque_baixo: 3                     # false, ou avisa quando cair para 3 ou menos
      preco: true                          # mudança de preço
      loja_fechada: true                   # fechou com senha / reabriu
      esgotou: true                        # responde ao alerta quando o produto esgota (padrão: desligado)
    filtros:
      incluir: [magnético, holo]           # só avisa se o nome tiver alguma dessas palavras
      excluir: [lixa, base, top coat]      # nunca avisa se o nome tiver alguma dessas palavras
    pausa_segundos: 1.5                    # pausa entre requisições nesta loja
    hashtag: Patty                         # hashtag da loja (padrão: gerada do nome → #MinhaLoja); aceita lista: [MarcaA, MarcaB]
    ativo: false                           # desliga a loja sem apagar a configuração
```

Só `url` é obrigatório. O que você não definir na loja vem da seção **`padrao`** no topo do arquivo. Para ligar o alerta de preço em **todas** as lojas, por exemplo, mude só ali:

```yaml
padrao:
  alertas:
    preco: true
```

**Sobre os filtros:**
- Não diferenciam maiúsculas, minúsculas nem acentos (`magnetico` encontra "Magnético").
- Valem só para os alertas. O monitor continua registrando todos os produtos, então, se você mudar o filtro depois, nada se perde.
- Se a loja definir `filtros`, eles substituem os filtros do `padrao`.

**Sobre os alertas:** dentro de `alertas`, a loja pode mudar só uma chave. As outras continuam vindo do `padrao`.

> ⚠️ O estado de cada loja é salvo pelo **domínio** (ex.: `state/lojawhatcha.com.br.json`). Mudar o `nome` não tem problema. Já mudar a `url` para outro domínio faz a loja ganhar uma nova linha de base.

### Plataformas suportadas

| Plataforma | Como lê | Observações |
|---|---|---|
| **Nuvemshop** | listagem `/produtos/page/N/?results_only=true` (cerca de 1 requisição a cada 20 produtos) | estoque exato por produto; estoque por variante vem da página do produto, aberta só quando necessário |
| **Shopify** | `/products.json` (250 produtos por requisição) | informa se cada variante está disponível, mas **não a quantidade**, então "últimas unidades" não funciona |

Para verificar a plataforma de uma loja antes de adicioná-la:

```bash
python -m monitor detect https://www.novaloja.com.br
```

Se aparecer "plataforma não reconhecida", a loja usa outra plataforma (Loja Integrada, Tray, Wix…) e precisa de um adaptador novo (veja [Adicionar uma plataforma](#-adicionar-uma-plataforma-nova)).

---

## 🧠 Como funciona

- **Catálogo completo a cada rodada.** Nas lojas Nuvemshop o `sitemap.xml` é gerado com atraso (produtos novos demoram horas para entrar e removidos continuam lá). Por isso o monitor lê a **listagem de produtos**, que traz o estoque de todos os itens, inclusive dos esgotados. Nenhum restock passa batido.
- **Variantes.** Na Nuvemshop o estoque por variante só existe na página do produto. Ela é aberta quando o produto é novo, quando volta ao estoque ou quando o estoque sobe. Produtos com várias variantes também são conferidos a cada 6 horas. Produtos com uma variante só (a grande maioria) não são reabertos.
- **Linha de base.** Na primeira vez que uma loja é lida, nada é avisado: o catálogo só é registrado.
- **Lojas fechadas com senha.** Quando a loja redireciona para `/password`, ela é marcada como fechada, sem gerar alertas falsos, e você recebe 🔒. Quando reabre, chega 🔓, junto com os alertas de produtos novos.
- **Produtos que somem.** Se um produto some da listagem em 2 varreduras completas seguidas, ele passa a ser considerado indisponível (algumas lojas escondem os esgotados). Se voltar, você recebe um restock.
- **Educado com os sites.** User-Agent identificável, pausa entre requisições, timeout e novas tentativas com espera crescente. Cada loja é lida em paralelo com as outras, mas as requisições dentro da mesma loja são em sequência.
- **Uma loja com erro não derruba as outras.** Se uma loja falhar 8 rodadas seguidas (~2h), chega um ⚠️ no Telegram.
- **Varredura incompleta.** Se uma página falhar no meio, nenhum produto é considerado removido nessa rodada.
- **Alertas que falham no envio** ficam em `state/_outbox.json` e são reenviados na rodada seguinte (por até 24h).

---

## 🤖 Configurar o Telegram

1. No Telegram, abra o **@BotFather**, mande `/newbot` e siga as instruções. Ele te dá um **token** (algo como `123456:ABC...`).
2. Abra a conversa com o seu bot e mande `/start`. Para receber os alertas num **grupo**, adicione o bot ao grupo e mande uma mensagem lá.
3. Copie `.env.example` para `.env` e cole o token em `TELEGRAM_TOKEN`.
4. Descubra o `chat_id`:
   ```bash
   python -m monitor chat-id
   ```
5. Coloque o número em `TELEGRAM_CHAT_ID` no `.env` e teste:
   ```bash
   python -m monitor test-telegram
   ```

> 🔐 O `.env` está no `.gitignore` e **nunca** deve ir para o GitHub. Lá, o token fica nos **Secrets**.

---

## 💻 Rodar localmente

Requer **Python 3.10+** (recomendado 3.12, igual ao GitHub Actions; no Mac: `brew install python@3.12`).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.txt -r requirements-dev.txt

python -m monitor                       # roda todas as lojas (envia alertas e salva o estado)
python -m monitor run --store whatcha   # só uma loja
python -m monitor run --dry-run         # mostra os alertas sem enviar e sem salvar nada
python -m pytest -q                     # testes

# Novidades dos últimos dias (só produtos novos: restocks passados não ficam registrados nas lojas)
python -m monitor recentes --dias 3            # só lista
python -m monitor recentes --dias 3 --enviar   # envia um alerta por produto, em ordem de lançamento
```

**Simular um alerta para testar:** edite `state/<loja>.json`.
- Restock: num produto disponível, troque `"available": true` por `false` e `"stock"` por `0`.
- Produto novo: apague a linha do produto.

Depois rode com `--dry-run` para ver o alerta, ou sem ele para receber no Telegram.

---

## ☁️ GitHub Actions

O workflow `.github/workflows/monitor.yml`:

- roda a cada 15 minutos e também manualmente (aba **Actions** → **Monitor de estoque** → **Run workflow**, com opção de rodar só uma loja ou em modo dry-run);
- instala as dependências, roda o monitor e **commita o estado** (`state/`) se ele mudou. Isso também mantém o repositório ativo, porque o GitHub desliga agendamentos de repositórios sem atividade há 60 dias;
- mostra um resumo por loja (produtos lidos, disponíveis, requisições, alertas) na página de cada execução.

**Secrets necessários** (Settings → Secrets and variables → Actions):

| Nome | Valor |
|---|---|
| `TELEGRAM_TOKEN` | token do @BotFather |
| `TELEGRAM_CHAT_ID` | número descoberto com `python -m monitor chat-id` |

Com o GitHub CLI:

```bash
gh secret set TELEGRAM_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

> ⏱️ O agendamento do GitHub não é confiável: neste repositório ele chegou a ficar horas sem disparar. Por isso o disparo principal vem de fora (seção abaixo), e o agendamento do GitHub fica só como reserva. Repositórios públicos não têm limite de minutos.

### Disparador externo (cron-job.org)

O [cron-job.org](https://cron-job.org) (gratuito) chama a API do GitHub a cada 15 minutos para rodar o monitor.

1. **Token do GitHub**, só com o mínimo necessário: [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
   - *Repository access*: **Only select repositories** → `polish-watcher`
   - *Permissions → Repository permissions → Actions*: **Read and write** (nada mais)
   - Com esse token só dá para disparar ou cancelar execuções. Não dá para ler os Secrets nem alterar o código.
2. **Tarefa no cron-job.org:**
   - URL: `https://api.github.com/repos/Warspite-git/polish-watcher/actions/workflows/monitor.yml/dispatches`
   - Agenda: a cada 15 minutos
   - Método: `POST`. Corpo: `{"ref":"main"}`
   - Cabeçalhos: `Accept: application/vnd.github+json`, `Authorization: Bearer <token>`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
   - Ligue o aviso por e-mail em caso de falha. A resposta esperada é **204**.
3. Quando o token expirar, gere outro e troque no cron-job.org. Coloque um lembrete na agenda.

---

## 🔐 Segurança

- **Segredos** só no `.env` local (ignorado pelo git) e nos **Secrets** do GitHub. O token do bot nunca aparece nos logs: o código o remove de qualquer mensagem de erro, e o GitHub mascara os Secrets.
- **Dependências com versão fixa e hash** (`requirements.txt` gerado com `pip-compile --generate-hashes`). Uma versão adulterada no PyPI não é instalada. O Dependabot abre PRs mensais com atualizações.
- **Actions fixadas por SHA**, com permissões mínimas. O token com permissão de escrita só existe no passo que faz o push do estado, não no passo que roda o monitor.
- **Cada loja só acessa o próprio domínio**: redirecionamentos para outros sites são recusados. As respostas têm limite de 20 MB e 120 s.
- **Textos vindos das lojas são higienizados**: nomes em uma linha só, sem caracteres de controle (evita injeção de comandos nos logs públicos do Actions), e escapados no HTML do Telegram.
- **O bot só funciona no seu grupo.** Ele só envia para o `TELEGRAM_CHAT_ID`, não responde a comandos e não lê as conversas. Se alguém adicioná-lo a outro grupo, ele sai sozinho na rodada seguinte. Isso só acontece se ele estiver no grupo configurado; se o seu grupo virar supergrupo, ele reconhece o grupo novo como seu. Em **canais** ele não sai (pode ser o seu próprio canal sendo montado): só registra um aviso no log com o ID do canal. Para impedir até o convite, desligue no @BotFather: Bot Settings → Allow Groups → off.
- **Grupo só de avisos:** se os membros não podem enviar mensagens, o bot precisa ser **administrador** do grupo (sem nenhuma permissão extra), senão ele também não consegue enviar.
- **Logs públicos:** como o repositório é público, qualquer pessoa vê os logs do Actions (nomes das lojas, contagens, erros). Nada sensível é registrado ali.

**Atualizar dependências manualmente:**

```bash
pip install pip-tools
pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements.txt requirements.in
pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements-dev.txt requirements-dev.in
```

---

## 🗂️ Estado

- `state/<domínio>.json`: catálogo conhecido de cada loja, com um produto por linha (os diffs dos commits ficam legíveis).
- Para **refazer a linha de base** de uma loja, apague o arquivo dela e faça commit.
- `state/_outbox.json`: alertas aguardando reenvio (só existe quando algum envio falhou).

---

## 🧩 Adicionar uma plataforma nova

1. Crie `monitor/adapters/minhaplataforma.py` com uma classe que herda de `Adapter` e implemente:
   - `detect(resp)`: devolve `True` se a página inicial for dessa plataforma;
   - `scan()`: devolve um `ScanResult` com todos os produtos (`Product`: id, nome, URL, imagem, preço, disponível, estoque);
   - opcionalmente `fetch_variants()`, se a listagem não trouxer as variantes.
2. Registre a classe em `ADAPTERS`, em `monitor/adapters/__init__.py`.
3. Inclua o nome em `PLATFORMS`, em `monitor/config.py`.

Toda a lógica de comparação, alertas, Telegram e estado é compartilhada.

---

## 🛠️ Solução de problemas

| Sintoma | O que fazer |
|---|---|
| Loja com ❌ no resumo do Actions | Abra o log da execução. Um `HTTP 403` costuma ser bloqueio do site; aumente `pausa_segundos` |
| "plataforma não reconhecida" | Defina `plataforma:` no `sites.yaml` ou crie um adaptador |
| "nenhum produto encontrado na listagem" | A loja Nuvemshop está vazia ou usa outro caminho. Tente `caminho_listagem: /productos/` (lojas em espanhol) |
| "linha de base adiada" | A primeira leitura falhou no meio; ela será refeita na próxima rodada |
| Muitos alertas repetidos | Verifique se o commit do estado está funcionando (passo "Salvar estado" no Actions) |
| Não chega nada no Telegram | Rode `python -m monitor test-telegram` localmente e confira os Secrets |

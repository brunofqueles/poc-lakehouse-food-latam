# Arquitetura — POC Lakehouse Food LATAM

Este documento detalha as decisões técnicas do projeto e o raciocínio por trás de cada uma. Para uma visão geral do projeto, consulte o [README](../README.md).

---

## 1. Visão geral da arquitetura

O projeto segue a **Arquitetura Medallion** (Raw → Bronze → Silver → Gold), com uma camada Raw temporária como diferencial de design frente ao modelo clássico de 3 camadas.

```
[Simulador de Dados - Faker]
        │
        │  gera arquivos JSON
        ▼
[Landing Zone - Unity Catalog Volume]
   (simula um Blob/ADLS externo)
        │
        │  Autoloader (batch diário, 1x/dia às 06:00)
        ▼
┌───────────────────────────────────────────────┐
│  RAW                                           │
│  - Dado bruto, fiel à origem (schema-on-read)  │
│  - TTL de 48h (expurgo automático)              │
│  - Particionado por data_ingestao               │
└───────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────┐
│  BRONZE                                        │
│  - Cópia fiel da Raw, sem casting/transformação │
│    (dado mantém o tipo/formato original)        │
│  - Metadados de controle (data_ingestao,        │
│    data_ingestao_bronze)                        │
│  - Particionado por pais + data_ingestao        │
└───────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────┐
│  SILVER                                        │
│  - Dimensões com SCD Tipo 2                     │
│    (dim_produtos, dim_lojas, dim_representantes)│
│  - dim_cambio (conversão de moeda, com fallback)│
│  - fato_vendas (grão: item vendido/transação)   │
│  - Fato particionado por pais + data_venda      │
└───────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────┐
│  GOLD                                          │
│  - Métricas e KPIs de negócio (em definição)    │
└───────────────────────────────────────────────┘
```

Todas as camadas utilizam **Delta Lake** como formato de armazenamento.

---

## 2. Por que uma camada Raw com TTL de 48h?

Em arquiteturas Medallion clássicas, a Raw normalmente é permanente. Neste projeto, optamos por dar à Raw um **tempo de vida curto (48h)**, simulando um padrão comum em ambientes corporativos regulados:

- Reduz custo de armazenamento de dados não tratados
- Reduz a superfície de exposição de dados brutos (antes de qualquer mascaramento/tratamento)
- Força a Bronze a ser a camada de retenção de longo prazo, mantendo o dado fiel à origem em formato Delta gerenciado — o que é mais seguro do ponto de vista de governança e auditoria (a Bronze é a "fonte da verdade" bruta, permanente)

O expurgo é feito via `DELETE`/`VACUUM` do Delta Lake, rodando como uma task dedicada dentro do job diário, logo após a ingestão Raw → Bronze ter sido concluída com sucesso.

### 2.1. Esclarecimento importante: TTL por data de ingestão, não por data de negócio

**Episódio real do desenvolvimento:** durante os testes, dados de vendas do dia 17/07 (`data_venda`) foram intencionalmente ingeridos na Raw de forma retroativa, no mesmo dia real em que os dados de 20/07 foram processados pela primeira vez (20/07). Ao rodar o expurgo alguns dias depois, os registros de 17/07 **não foram removidos**, mesmo já representando uma venda "antiga" do ponto de vista de negócio.

**Explicação:** a coluna que controla o TTL (`data_ingestao_particao`) reflete **quando o dado entrou fisicamente na Raw**, não a data da transação de negócio (`data_venda`). Como os dados de 17/07 e 20/07 foram ambos processados no mesmo dia real (20/07), ambos compartilham a mesma partição de ingestão — portanto, ainda não haviam completado 48 horas **desde a ingestão**, independentemente da data da venda em si.

**Isso é o comportamento correto e intencional:** o TTL de 48h protege contra acúmulo de dados **não processados** na Raw por muito tempo (o problema operacional que a Raw temporária busca resolver), não é uma regra sobre a idade do evento de negócio. Em operação normal (dados processados no mesmo dia em que são gerados), essa distinção não seria perceptível — ela só se tornou visível neste projeto por causa de cargas retroativas manuais feitas durante o desenvolvimento/teste.

### 2.2. TTL lógico (48h) vs. retenção física do VACUUM (7 dias)

O `DELETE` que implementa o TTL de 48h é uma remoção **lógica** — o Delta Lake mantém os arquivos físicos por um período mínimo de segurança (padrão: 7 dias), controlado pela trava `spark.databricks.delta.retentionDurationCheck.enabled`, que impede a execução de `VACUUM` com retenção menor que esse limite. Essa proteção existe para evitar remoção física de arquivos que processos de leitura concorrentes (ex: streaming) ainda possam precisar acessar.

**Decisão:** manter a retenção padrão de 7 dias para o `VACUUM`, em vez de desabilitar a trava de segurança para forçar uma limpeza física em 48h. Isso significa que o objetivo principal do TTL (impedir que dados brutos fiquem visíveis/consultáveis por muito tempo) é cumprido integralmente pelo `DELETE` em 48h; a liberação física de espaço em disco ocorre de forma mais conservadora, em até 7 dias — um trade-off aceitável entre economia de armazenamento e segurança operacional, priorizando a segunda.

---

## 3. Por que Delta Lake em vez de Parquet puro?

Parquet puro foi meu contraponto avaliado, mas descartado porque não oferece nativamente:

- **MERGE INTO (upsert)** — essencial para implementar SCD Tipo 2 nas dimensões sem reescrever manualmente partições inteiras
- **Transações ACID** — evita corrupção de dados em escritas concorrentes/batch
- **VACUUM** — necessário para o expurgo automático da camada Raw (48h)
- **Time Travel** — útil para auditoria e debug

Delta Lake é uma camada sobre o próprio Parquet (adiciona um log de transações `_delta_log`), então não há perda de compatibilidade — apenas ganho de funcionalidade, nativo do Databricks e sem custo adicional no Free Edition.

---

## 4. Por que SCD Tipo 2 nas dimensões?

Avaliamos SCD Tipo 1 (sobrescreve, sem histórico) vs. SCD Tipo 2 (mantém histórico via versionamento de linhas).

Optamos por **SCD Tipo 2** em Produtos, Lojas e Representantes porque, no cenário simulado de uma empresa real de alimentos LATAM, atributos como região de um representante, responsável de um centro de distribuição ou preço de um produto mudam ao longo do tempo — e a análise de negócio precisa refletir o contexto **vigente na data da venda**, não o contexto atual. SCD Tipo 1 perderia essa rastreabilidade histórica.

---

## 5. Modelagem de dados — schemas detalhados

O grão da fato Vendas é: **1 linha = 1 item vendido por transação** (produto+tamanho, quantidade, valor, país, centro, representante, data). Essa granularidade permite qualquer agregação futura na camada Gold (por produto, por região, por representante, por período) sem perda de detalhe.

### `dim_produtos`

| Coluna | Tipo | Descrição |
|---|---|---|
| `produto_id` | string | Chave natural |
| `nome_interno` | string | Chave técnica neutra (ex: "MAIONESE"), usada para gerar o `sku` |
| `nome_brasil` | string | Nome em português (ex: "Maionese") |
| `nome_argentina` | string | Nome em espanhol argentino (ex: "Mayonesa") |
| `nome_mexico` | string | Nome em espanhol mexicano (ex: "Mayonesa") |
| `nome_ingles` | string | Nome em inglês, usado exclusivamente na camada Gold (ex: "Mayonnaise") |
| `tamanho` | string | "1kg" ou "5kg" |
| `sku` | string | Combinação de `nome_interno` + tamanho |
| `preco_brasil_brl` | decimal | Preço em Real |
| `preco_argentina_ars` | decimal | Preço em Peso argentino |
| `preco_mexico_mxn` | decimal | Preço em Peso mexicano |
| `data_inicio` / `data_fim` / `flag_ativo` | date / date / boolean | Controle SCD2 |

**Nota de idioma:** cada país tem produtos com nomenclatura local diferente (ex: Ketchup é popularmente chamado de "Catsup" no México) — mantido fielmente por país nas colunas `nome_brasil`/`nome_argentina`/`nome_mexico`, com uma tradução adicional para inglês (`nome_ingles`) reservada ao consumo executivo na Gold.

### `dim_lojas` (Centros de Distribuição)

| Coluna | Tipo | Descrição |
|---|---|---|
| `loja_id` | string | Chave natural |
| `nome_cidade` | string | Cidade do centro de distribuição |
| `pais` | string | Brasil / Argentina / México |
| `supervisor_responsavel` | string | Nome do supervisor (referência lógica à dim_representantes) |
| `peso_distribuicao` | decimal | Peso/percentual usado pelo simulador para distribuir vendas |
| `data_inicio` / `data_fim` / `flag_ativo` | date / date / boolean | Controle SCD2 |

### `dim_representantes`

| Coluna | Tipo | Descrição |
|---|---|---|
| `representante_id` | string | Chave natural |
| `nome` | string | Gerado via Faker (locale do país) |
| `cargo` | string | "Gerente" ou "Supervisor" |
| `pais` | string | País de atuação |
| `centro_vinculado` | string | Centro ao qual está vinculado (nulo para Gerente, que atua por país) |
| `data_inicio` / `data_fim` / `flag_ativo` | date / date / boolean | Controle SCD2 |

### `fato_vendas`

| Coluna | Tipo | Descrição |
|---|---|---|
| `venda_id` | string | Chave da transação (UUID) |
| `data_venda` | date | Data simulada da venda |
| `produto_id` | string | FK para dim_produtos (versão vigente na data) |
| `loja_id` | string | FK para dim_lojas |
| `representante_id` | string | FK para dim_representantes |
| `pais` | string | Redundante proposital (facilita particionamento/filtro) |
| `quantidade` | int | Unidades vendidas |
| `valor_unitario_moeda_local` | decimal | Preço no momento da venda, moeda local |
| `valor_total_moeda_local` | decimal | quantidade × valor_unitario |
| `moeda` | string | BRL / ARS / MXN |
| `cambio_usado` | decimal | Taxa de câmbio aplicada (rastreabilidade) |
| `valor_total_usd` | decimal | Convertido via `dim_cambio` |
| `data_ingestao` | timestamp | Metadado técnico de controle |

---

## 5.1. Estratégia de idiomas por camada

Como o projeto opera em 3 países com idiomas diferentes (português do Brasil, espanhol argentino, espanhol mexicano) e o consumidor final da Gold é um perfil executivo (CFO) baseado nos EUA, foi definida uma estratégia explícita de idioma por camada:

| Camada | Idioma | Justificativa |
|---|---|---|
| Raw / Bronze / Silver | Nativo de cada país (pt-BR, es-AR, es-MX) | Representa fielmente o dado operacional local, útil para times de Engenharia/Analytics de cada região |
| Gold | Inglês (valores **e** nomes de colunas) | Camada de consumo executivo/internacional — tabelas e valores traduzidos para atender relatórios globais |

Essa decisão evita que a camada de negócio (Gold) misture "Maionese", "Mayonesa" e "Mostaza" no mesmo relatório para um público que não opera nesses idiomas — centralizando a tradução em um único ponto (a dimensão de produtos) em vez de espalhar lógica de tradução pelo pipeline.

---

## 5.2. Simulador de dados — decisões de implementação

Esta seção documenta decisões técnicas tomadas durante a construção do simulador (`src/simulador/`), que impactam diretamente o que as camadas seguintes (Bronze/Silver) precisam tratar.

### Chaves técnicas neutras (evitando "smart keys")

O `representante_id` usa o prefixo único `REP` para todos os cargos (Gerente e Supervisor), em vez de prefixos como `GER`/`SUP`. Isso segue a prática recomendada de modelagem de dados de **evitar chaves inteligentes** (IDs que embutem significado de negócio) — se o cargo de uma pessoa mudar no futuro, a chave primária permanece estável, sem quebrar referências históricas em `fato_vendas`. O cargo é corretamente representado na coluna `cargo`, não na chave.

### Simulação de qualidade de dados (sujeira proposital)

Para dar propósito real de tratamento de dados à camada Silver, o simulador introduz inconsistências propositais e controladas:

- **Nomes de representantes** (`FakerHelper.gerar_nome()`): 15% de chance de vir em CAIXA ALTA ou com espaços extras nas bordas — simula variações comuns de cadastro manual
- **Preços de produtos** (`dim_produtos`): gravados como **texto**, no formato brasileiro/latino (vírgula decimal, ex: `"12,90"`), simulando uma extração real de ERP — a Bronze precisará fazer o casting explícito para tipo decimal
- **Nomes de cidade**: gravados **com acentuação correta** na origem (decisão consciente, revertendo a ideia inicial de simular perda de encoding) — porém a camada Silver vai **remover acentos e padronizar para maiúsculas**, como regra de qualidade para uso em chaves de junção/agrupamento

### Separação entre dado transacional e cálculo derivado

O simulador de vendas grava apenas os dados "brutos" da transação (`quantidade`, `valor_unitario_moeda_local` como texto, `moeda`) — **não calcula** `valor_total_moeda_local`, `cambio_usado` nem `valor_total_usd`. Esses campos são cálculos/enriquecimentos derivados, responsabilidade da Bronze (casting) e Silver (cálculo de total e conversão de câmbio), mantendo a Raw fiel a uma exportação real de sistema de origem, que não faria esse tipo de processamento.

### Regra de negócio: vínculo representante-centro

Uma venda gerada para um centro de distribuição específico só pode ser atribuída ao Supervisor daquele centro, ou ao Gerente do país (que supervisiona todos os centros) — nunca a um Supervisor de outro centro. Essa regra é aplicada diretamente na geração da venda, filtrando os representantes elegíveis por `centro_vinculado` antes da seleção aleatória.

### Regra de negócio: dias úteis e feriados

O simulador de vendas não gera transações em finais de semana ou feriados nacionais, usando a biblioteca `holidays` (calendários de Brasil, Argentina e México). Cada execução do notebook registra um **log de auditoria cumulativo** (`logs/execucao_vendas`, modo *append*) contendo data, país, se era dia útil, e o motivo do bloqueio quando aplicável — permitindo rastrear todas as execuções do pipeline, inclusive as que não geraram vendas.

### Estrutura de pastas da Landing Zone

```
/Volumes/poc_latam_food/landing/blob_simulado/
├── dimensoes/
│   ├── produtos/
│   ├── lojas/
│   └── representantes/
├── vendas/
│   └── pais=<pais>/data=<data_venda>/     (Hive-style partitioning)
└── logs/
    └── execucao_vendas/
```

O particionamento `pais=X/data=Y` em vendas segue o padrão Hive-style, permitindo que ferramentas de leitura (como o Autoloader) filtrem por partição sem precisar ler o conteúdo dos arquivos.

### Bronze como cópia fiel (sem casting)

Diferente de uma definição mais comum de Bronze (que às vezes já aplica tipos/schema), neste projeto a Bronze foi definida como uma **cópia fiel da Raw em formato Delta gerenciado, sem nenhuma transformação de conteúdo** — os preços permanecem como texto (`"12,90"`), os nomes mantêm qualquer inconsistência vinda da origem. Apenas metadados de controle (ex: `data_ingestao_bronze`) são adicionados. Todo casting de tipo, padronização de texto, cálculo de totais e conversão de câmbio é responsabilidade exclusiva da **Silver**. Essa escolha reforça a Bronze como a "fonte da verdade" bruta e imutável do dado, com toda transformação centralizada em uma única camada (Silver), facilitando auditoria e rastreabilidade.

### Inferência automática de schema (Autoloader) trata números como texto

**Episódio real do desenvolvimento:** a coluna `peso_distribuicao` de `dim_lojas`, embora tenha sido gerada como valor numérico (`double`) no simulador, chegou como `string` na Raw/Bronze. Isso ocorre porque o Autoloader **infere o schema automaticamente a partir do JSON**, e essa inferência, em alguns casos, não distingue com precisão valores numéricos simples de texto — resultando em colunas numéricas sendo tratadas como string mesmo sem essa ser uma "sujeira" proposital do projeto.

**Implicação prática:** toda coluna numérica que chega da Bronze precisa ser **conferida explicitamente** (via `printSchema()`) na Silver antes de ser usada em cálculos, mesmo quando não fazia parte da lista de campos com "sujeira" proposital documentada na seção 5.2. O casting explícito (`.cast("double")`, `.cast("decimal(...)")`) deve ser aplicado sempre que necessário, independentemente de a coluna ter sido projetada como numérica na origem.

### Padronização de texto na Silver: maiúsculas totais, sem exceção para nomes de pessoas

**Decisão revisada durante o desenvolvimento:** a intenção inicial era manter a acentuação em nomes de representantes (`dim_representantes`) para fins de exibição, diferente do tratamento dado a nomes de cidade (que já seriam padronizados como chave de junção). Essa decisão foi **revista**: como a Silver também alimenta consumidores analíticos (ex: um time de Ciência de Dados), e não apenas relatórios executivos, optou-se por padronizar **todas** as colunas de texto na Silver — maiúsculas, sem acento — incluindo nomes de pessoas.

**Achado adicional:** a geração de nomes via Faker, em alguns casos, incluiu títulos/prefixos de tratamento (ex: "Srta.", "Lic.") como parte do nome gerado, dependendo do locale. Esses títulos foram removidos via expressão regular antes da padronização, evitando poluir o nome com informação que não é o dado de interesse (o nome da pessoa em si).

**Onde a exibição "bonita" acontece:** apenas na camada **Gold**, que é a única camada pensada para consumo executivo/apresentação — reforçando a separação de responsabilidades já estabelecida na seção 5.1 (estratégia de idiomas por camada).

---

## 6. Distribuição simulada de vendas

| País | % do volume total | Regra de distribuição entre centros |
|---|---|---|
| Brasil | 70% | São Paulo com peso fixo maior; Rio de Janeiro e Minas Gerais oscilando entre si |
| México | 25% | Guadalajara e Cidade do México oscilando entre si, sem líder fixo |
| Argentina | 5% | Buenos Aires (centro único) |

Sazonalidade: pico de **+30%** no volume de vendas no dia 24/12 (Natal), único evento sazonal simulado no projeto.

---

## 7. Câmbio: API externa com fallback

Testamos a conectividade de saída do Databricks Free Edition com sucesso, confirmando que chamadas HTTP a APIs externas funcionam no ambiente serverless. A fonte de câmbio escolhida foi a API pública [exchangerate-api.com](https://www.exchangerate-api.com/).

**Estratégia de resiliência:** como toda dependência externa pode falhar (rate limit, timeout, instabilidade), o pipeline não deve travar por conta disso. Caso a chamada falhe, a tabela `dim_cambio` retém a **última cotação obtida com sucesso**, sinalizando o registro como desatualizado, em vez de interromper a execução do pipeline inteiro.

**Cotação única diária (sem variação intraday):** a tabela `dim_cambio` armazena **uma única cotação por moeda, por dia** — capturada no momento da execução do Workflow (job diário às 06:00), não múltiplas capturas ao longo do dia. Embora o câmbio real oscile intraday, essa simplificação é consciente e reflete uma prática comum em contabilidade e relatórios financeiros corporativos (ex: a taxa PTAX do Banco Central do Brasil, usada como referência única diária para conversões oficiais) — priorizando consistência e auditabilidade sobre precisão de tempo real.

**Granularidade histórica:** diferente de uma tabela que apenas sobrescreve a cotação mais recente, `dim_cambio` mantém o **histórico completo** (uma linha por moeda/dia), permitindo consultar qual era a cotação vigente em qualquer data passada — essencial para que vendas antigas sejam convertidas com a taxa correta da época, não com a taxa atual.

**Limitação conhecida (vendas anteriores à existência de `dim_cambio`):** como `dim_cambio` só passou a existir a partir do dia em que o notebook `silver_cambio.py` foi executado pela primeira vez, vendas com `data_venda` anterior a essa data não encontram cotação correspondente no JOIN com `silver.fato_vendas` — os campos `cambio_usado` e `valor_total_usd` permanecem nulos para esses registros. Optou-se conscientemente por **não preencher retroativamente** esses valores com a cotação atual, por não representar a taxa real vigente naquelas datas — mantendo a integridade e honestidade do dado em vez de uma aproximação artificial.

---

## 8. Particionamento por camada

| Camada | Estratégia | Justificativa |
|---|---|---|
| Raw | `data_ingestao` | Facilita o expurgo por partição inteira (mais barato que filtro linha a linha) |
| Bronze | `pais` + `data_ingestao` | Mantém rastreabilidade da origem com granularidade adequada |
| Silver (fato) | `pais` + `data_venda` | Otimiza leituras filtradas por região, dado o volume desigual entre países |
| Silver (dimensões) | Sem partição | Volume baixo (poucas dezenas de registros + histórico) não justifica partição |
| Gold | `pais` (em `gold_sales_by_country`) | `gold_sales_global` não particiona, dado o baixo volume agregado |

### Tabelas Gold — implementação

| Tabela | Granularidade | Colunas (em inglês) | Uso |
|---|---|---|---|
| `gold.sales_by_country` | País + dia (`period`) | `country`, `period`, `total_local_currency`, `local_currency_code`, `total_usd` | CFO analisa o resultado de cada país, na moeda local e em USD |
| `gold.sales_global` | Dia (`period`), consolidado | `period`, `total_usd` | CFO analisa o total global consolidado, somente em USD |
| `gold.sales_by_product` | Produto + tamanho + país + dia (`period`) | `product_name`, `size`, `country`, `period`, `quantity_sold`, `total_usd` | CFO analisa performance de vendas por SKU (ex: unidades e receita de Mayonnaise 1kg no Brasil) |

**Terceira tabela adicionada durante o desenvolvimento:** o plano original previa apenas 2 tabelas Gold. `gold.sales_by_product` foi adicionada ao se identificar uma lacuna real: nenhuma das duas tabelas originais permitia analisar vendas por produto — informação essencial para um relatório executivo de uma empresa de bens de consumo. A tabela reaproveita a tradução de nome de produto (`nome_ingles`) já preparada na dimensão desde a modelagem inicial (seção 5), mas que ainda não era utilizada em nenhuma tabela Gold.

**Decisão: sales_by_product não inclui moeda local.** Diferente de `sales_by_country`, esta tabela expõe apenas `total_usd`, sem `total_local_currency`/`local_currency_code`. A justificativa é de clareza executiva: o relatório de produto é consumido já em nível consolidado (dólar), e a redundância de mostrar moeda local ao lado do país (que já a identifica implicitamente) adicionaria ruído visual sem valor analítico adicional.

**Granularidade escolhida:** diária (não mensal), para permitir validação imediata com o volume de dados ainda pequeno do projeto. Agregações mensais podem ser obtidas facilmente a partir dessas tabelas diárias, se necessário no futuro.

**Estratégia de escrita:** todas as tabelas usam **overwrite completo** a cada execução (`mode("overwrite")`, com `overwriteSchema=true`), recalculando tudo a partir da Silver — em vez de atualização incremental. Essa escolha simplifica a lógica (a Gold é sempre um reflexo fiel e current da Silver) e evita inconsistências caso dados históricos na Silver sejam corrigidos ou reprocessados.

**Tradução de país:** realizada via `CASE WHEN` (função `when()` do PySpark) no momento da consulta, mapeando os valores padronizados da Silver (`BRASIL`, `ARGENTINA`, `MEXICO`) para os nomes em inglês (`Brazil`, `Argentina`, `Mexico`) esperados na Gold.

**Decisão: visão de somatório por país sem quebra por dia não vira tabela própria.** Uma visão adicional (total consolidado por país, somando todos os dias) foi considerada, mas optou-se por mantê-la como uma **consulta simples** sobre `gold.sales_by_country` (agrupando sem a coluna `period`), em vez de criar uma quarta tabela Gold persistente — evitando redundância de dados quando uma consulta trivial já resolve a necessidade.

**Cuidado ao agregar valores em USD nessa visão consolidada:** como nem todos os dias possuem cotação de câmbio disponível (ver seção 7), uma soma de `total_usd` que misture dias com e sem câmbio representaria apenas uma fração do período real, podendo induzir a uma leitura equivocada. Por esse motivo, a consulta de somatório por país inclui apenas o total em moeda local, omitindo o total em USD.

---

## 9. Governança e Controle de Acesso (RBAC) — Modelo Pretendido

> **Nota de ambiente:** o Databricks Free Edition opera com um único usuário e não oferece administração multiusuário completa (Account Console com múltiplos membros/grupos). Por isso, o RBAC (Role-Based Access Control) abaixo é um **modelo de especificação** — representa como a governança de acesso seria implementada em um ambiente de produção real (tier Premium/Enterprise com múltiplos usuários), e não uma configuração tecnicamente ativa neste projeto.

### Estrutura de Unity Catalog

| Nível | Nome | Descrição |
|---|---|---|
| Catalog | `poc_latam_food` | Catalog raiz do projeto, contendo todas as camadas |
| Schema | `landing` | Contém o Volume da Landing Zone (simulação do Blob externo) |
| Schema | `bronze` / `silver` / `gold` | Tabelas Delta de cada camada da arquitetura Medallion |

### Times e permissões pretendidas

| Time | Papel no projeto | Permissões pretendidas |
|---|---|---|
| **Arquitetura** | Define padrões, aprova mudanças estruturais | `ALL PRIVILEGES` no catalog (admin) — criação/alteração de schemas, políticas de acesso, aprovação de mudanças de modelagem |
| **Engenharia de Dados** | Constrói e mantém os pipelines | `USE CATALOG`, `USE SCHEMA`, `CREATE TABLE`, `MODIFY` em `landing`, `bronze` e `silver` — acesso de leitura/escrita nas camadas que constrói e mantém |
| **Gestão / Negócio** | Consome métricas para tomada de decisão | `SELECT` (somente leitura) restrito ao schema `gold` — acesso apenas às métricas já consolidadas, sem visibilidade de dados brutos/intermediários |
| **Analytics / BI** | Constrói dashboards e análises exploratórias | `SELECT` (somente leitura) em `silver` e `gold` — precisa de mais granularidade que a Gestão, mas não deve alterar dados |

### Princípios aplicados (mesmo que não implementados tecnicamente aqui)

- **Least Privilege (menor privilégio)**: cada time recebe apenas o acesso mínimo necessário para sua função
- **Segregação por camada**: dados brutos (Raw/Bronze) ficam restritos à Engenharia; times de negócio só acessam dados já tratados e validados (Silver/Gold)
- **Somente leitura para consumo**: times de Gestão e Analytics nunca têm permissão de escrita, evitando alteração acidental de dados de origem

---

## 10. Orquestração

Job diário, agendado às 06:00, com dependências encadeadas por estágio.

**Evolução do plano original:** o desenho inicial deste documento previa 6 tasks (uma por camada conceitual: Simulador, Raw, Bronze, Expurgo Raw, Silver, Gold). Conforme o projeto evoluiu, cada camada foi modularizada em múltiplos notebooks independentes (para reforçar boas práticas de responsabilidade única e reuso). Isso resultou em **16 tasks reais**, mantendo a mesma sequência lógica de 6 estágios, porém com granularidade fina — permitindo identificar exatamente qual notebook falhou, em vez de apontar apenas para a camada como um todo.

### Estrutura de tasks e dependências

```
gerar_vendas_brasil ─┐
gerar_vendas_argentina ─┼──► ingestao_raw_dimensoes ──► bronze_dimensoes ─┬──► expurgo_raw (paralelo à Silver)
gerar_vendas_mexico ─┘        ingestao_raw_vendas ──► bronze_vendas ─────┘
                                                                          │
                              bronze_dimensoes ──► silver_produtos ──────┤
                                              ──► silver_lojas ──────────┤
                                              ──► silver_representantes ┤
                              bronze_dimensoes + bronze_vendas ──► silver_cambio ┤
                                                     silver_fato_vendas (depende das 4 acima)
                                                     │
                              silver_fato_vendas ──► gold_sales_by_country
                                                  ──► gold_sales_global
                                                  ──► gold_sales_by_product
```

**Nota sobre `expurgo_raw`:** embora conceitualmente pertença ao "estágio" entre Bronze e Silver, tecnicamente não há dependência de dados entre o expurgo e o processamento da Silver — ambos dependem apenas da Bronze estar concluída. Por isso, `expurgo_raw` foi estruturado para rodar **em paralelo** às tasks da Silver, não como um bloqueio sequencial — reduzindo pontos de falha desnecessários no pipeline (uma falha no expurgo não impede o processamento da Silver).

**Lista completa das 16 tasks:**
1-3. `gerar_vendas_brasil`, `gerar_vendas_argentina`, `gerar_vendas_mexico` (paralelas)
4-5. `ingestao_raw_dimensoes`, `ingestao_raw_vendas`
6-7. `bronze_dimensoes`, `bronze_vendas`
8. `expurgo_raw`
9-12. `silver_produtos`, `silver_lojas`, `silver_representantes`, `silver_cambio` (paralelas entre si)
13. `silver_fato_vendas` (depende das 4 anteriores)
14-16. `gold_sales_by_country`, `gold_sales_global`, `gold_sales_by_product` (paralelas entre si)

**Nota de correção de design:** a ordem original deste documento posicionava o Expurgo Raw antes da Bronze, o que causaria perda de dados não processados. Corrigido para garantir que o expurgo só ocorra após a cópia bem-sucedida dos dados para a camada seguinte.

Alertas de falha configurados para dois destinatários (e-mail pessoal + e-mail simulando um grupo de trabalho), reforçando uma prática de observabilidade mínima esperada em pipelines de produção.

### 10.1. Job auxiliar temporário — geração diária de vendas

**Contexto:** durante a fase de desenvolvimento incremental do pipeline (construção manual e testada camada por camada), identificou-se um risco operacional: a geração diária de vendas depende de execução manual do notebook `gerar_vendas.py`, criando risco de esquecimento e, consequentemente, "buracos" na sequência de datas — o que prejudicaria os testes das camadas seguintes (Bronze/Silver/Gold).

**Decisão:** criar um Job auxiliar, temporário, contendo **apenas** a execução diária automatizada de `gerar_vendas.py` (uma task por país: Brasil, Argentina, México), agendado às 06:00 — mitigando o risco de descontinuidade de dados sem antecipar a complexidade de orquestrar camadas ainda não construídas.

**Natureza temporária:** este job é uma **medida de contenção**, não a solução definitiva de orquestração. Ele será **descomissionado** assim que o Workflow completo de 6 tasks (Simulador → Raw → Bronze → Expurgo Raw → Silver → Gold) estiver construído e testado, na etapa correspondente do roadmap do projeto.

**Justificativa da abordagem:** soluções-ponte durante construção incremental de pipelines são uma prática comum em ambientes de produção reais — permitem mitigar um risco operacional concreto e imediato sem acoplar a solução a componentes que ainda não existem, mantendo o desenvolvimento das demais camadas desacoplado e testável de forma independente.

**Lacuna identificada na prática (episódio real do desenvolvimento):** a automação parcial (apenas o simulador) resolveu o risco de esquecimento na *geração* dos dados, mas expôs uma lacuna relacionada: como a ingestão Raw (`ingestao_raw_vendas.py`) e a cópia Bronze (`bronze_vendas.py`) continuaram sendo executadas manualmente durante o desenvolvimento, os dados gerados automaticamente pelo job auxiliar em dois dias (21 e 22/07) permaneceram **apenas na Landing Zone**, sem chegar às tabelas Raw/Bronze, até serem processados manualmente dias depois. O Autoloader e o Structured Streaming sobre Delta lidaram corretamente com esse atraso — ao serem executados novamente, retomaram do checkpoint e processaram exatamente o incremento pendente, sem duplicar nem perder dados. Esse episódio reforça, na prática, por que a automação parcial é insuficiente a médio prazo, e valida a necessidade do Workflow completo (etapa 22), que vai encadear as 6 tasks (incluindo Raw e Bronze) de forma automática e sequencial, eliminando esse tipo de lacuna.

---

## 11. Decisões técnicas complementares (a incorporar durante o desenvolvimento)

- **Databricks Widgets**: parametrização de notebooks (ex: `pais` como parâmetro, permitindo reuso do mesmo notebook para Brasil, Argentina e México em vez de 3 notebooks duplicados)
- **Programação Orientada a Objetos**: lógica reutilizável centralizada em `src/utils` (ex: uma classe `SCD2Handler` usada pelos 3 notebooks de dimensão)
- **Infrastructure as Code (IaC)**: avaliação do uso de Databricks Asset Bundles para definir o Workflow como código versionado, em vez de configuração manual pela interface

---

## 12. Estimativa de custos

Ver seção específica no [README](../README.md#-estimativa-de-custos-simulação), com simulações comparativas entre Databricks sobre AWS e Azure Databricks, incluindo evidências em [`docs/evidencias/`](evidencias/).
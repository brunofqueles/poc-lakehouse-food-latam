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
│  - Schema aplicado                              │
│  - Metadados de controle (data_ingestao,        │
│    arquivo_origem)                              │
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
- Força a Bronze a ser a camada de retenção de longo prazo, já com schema aplicado — o que é mais seguro do ponto de vista de governança

O expurgo é feito via `DELETE`/`VACUUM` do Delta Lake, rodando como uma task dedicada dentro do job diário, logo após a ingestão Raw → Bronze ter sido concluída com sucesso.

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

---

## 8. Particionamento por camada

| Camada | Estratégia | Justificativa |
|---|---|---|
| Raw | `data_ingestao` | Facilita o expurgo por partição inteira (mais barato que filtro linha a linha) |
| Bronze | `pais` + `data_ingestao` | Mantém rastreabilidade da origem com granularidade adequada |
| Silver (fato) | `pais` + `data_venda` | Otimiza leituras filtradas por região, dado o volume desigual entre países |
| Silver (dimensões) | Sem partição | Volume baixo (poucas dezenas de registros + histórico) não justifica partição |
| Gold | `pais` (em `gold_sales_by_country`) | `gold_sales_global` não particiona, dado o baixo volume agregado |

### Tabelas Gold planejadas (pré-definição, detalhamento na etapa 21)

| Tabela | Granularidade | Colunas (em inglês) | Uso |
|---|---|---|---|
| `gold_sales_by_country` | País + período | `country`, `period`, `total_local_currency`, `local_currency_code`, `total_usd` | CFO analisa o resultado de cada país, na moeda local e em USD |
| `gold_sales_global` | Período (consolidado) | `period`, `total_usd` | CFO analisa o total global consolidado, somente em USD |

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

Job diário, agendado às 06:00, com 6 tasks encadeadas (dependência sequencial):

1. **Simulador** — gera arquivos JSON na Landing Zone
2. **Raw** — Autoloader lê a Landing Zone e grava na Raw
3. **Bronze** — aplica schema e metadados de controle
4. **Expurgo Raw** — remove registros de vendas com mais de 48h (executado **após** a Bronze, garantindo que nenhum dado seja perdido antes de ser processado pela camada seguinte)
5. **Silver** — MERGE SCD2 nas dimensões + carga incremental da fato Vendas
6. **Gold** — cálculo de métricas de negócio

**Nota de correção de design:** a ordem original deste documento posicionava o Expurgo Raw antes da Bronze, o que causaria perda de dados não processados. Corrigido para garantir que o expurgo só ocorra após a cópia bem-sucedida dos dados para a camada seguinte.

Alertas de falha configurados para dois destinatários (e-mail pessoal + e-mail simulando um grupo de trabalho), reforçando uma prática de observabilidade mínima esperada em pipelines de produção.

### 10.1. Job auxiliar temporário — geração diária de vendas

**Contexto:** durante a fase de desenvolvimento incremental do pipeline (construção manual e testada camada por camada), identificou-se um risco operacional: a geração diária de vendas depende de execução manual do notebook `gerar_vendas.py`, criando risco de esquecimento e, consequentemente, "buracos" na sequência de datas — o que prejudicaria os testes das camadas seguintes (Bronze/Silver/Gold).

**Decisão:** criar um Job auxiliar, temporário, contendo **apenas** a execução diária automatizada de `gerar_vendas.py` (uma task por país: Brasil, Argentina, México), agendado às 06:00 — mitigando o risco de descontinuidade de dados sem antecipar a complexidade de orquestrar camadas ainda não construídas.

**Natureza temporária:** este job é uma **medida de contenção**, não a solução definitiva de orquestração. Ele será **descomissionado** assim que o Workflow completo de 6 tasks (Simulador → Raw → Bronze → Expurgo Raw → Silver → Gold) estiver construído e testado, na etapa correspondente do roadmap do projeto.

**Justificativa da abordagem:** soluções-ponte durante construção incremental de pipelines são uma prática comum em ambientes de produção reais — permitem mitigar um risco operacional concreto e imediato sem acoplar a solução a componentes que ainda não existem, mantendo o desenvolvimento das demais camadas desacoplado e testável de forma independente.

---

## 11. Decisões técnicas complementares (a incorporar durante o desenvolvimento)

- **Databricks Widgets**: parametrização de notebooks (ex: `pais` como parâmetro, permitindo reuso do mesmo notebook para Brasil, Argentina e México em vez de 3 notebooks duplicados)
- **Programação Orientada a Objetos**: lógica reutilizável centralizada em `src/utils` (ex: uma classe `SCD2Handler` usada pelos 3 notebooks de dimensão)
- **Infrastructure as Code (IaC)**: avaliação do uso de Databricks Asset Bundles para definir o Workflow como código versionado, em vez de configuração manual pela interface

---

## 12. Estimativa de custos

Ver seção específica no [README](../README.md#-estimativa-de-custos-simulação), com simulações comparativas entre Databricks sobre AWS e Azure Databricks, incluindo evidências em [`docs/evidencias/`](evidencias/).

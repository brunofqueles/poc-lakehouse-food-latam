# POC Lakehouse — Food LATAM

Projeto de portfólio em Engenharia de Dados que simula a plataforma de dados de uma empresa de alimentos com operação na América Latina (Brasil, Argentina e México). Demonstra, de ponta a ponta, um pipeline de dados profissional: da geração de dados sintéticos até a disponibilização de métricas de negócio para consumo executivo, seguindo padrões reais de mercado — incluindo desafios técnicos reais encontrados e corrigidos durante o desenvolvimento.

> **Status:** ✅ Pipeline completo e funcional (Simulador → Raw → Bronze → Silver → Gold), orquestrado via Databricks Workflows.

---

## 🎯 Visão geral do negócio (simulado)

A empresa fictícia distribui 3 produtos (Maionese, Mostarda e Ketchup, cada um em embalagens de 1kg e 5kg) através de 6 centros de distribuição em 3 países:

| País | Centros de Distribuição |
|---|---|
| 🇧🇷 Brasil | Rio de Janeiro, São Paulo, Minas Gerais |
| 🇦🇷 Argentina | Buenos Aires |
| 🇲🇽 México | Guadalajara, Cidade do México |

A operação é liderada por 3 Gerentes (1 por país) e 6 Supervisores (1 por centro de distribuição), totalizando 9 representantes comerciais.

**Distribuição simulada de vendas:**
- Brasil: 70% do volume total (São Paulo com peso maior e fixo; Rio de Janeiro e Minas Gerais oscilando entre si)
- México: 25% do volume total (Guadalajara e Cidade do México oscilando entre si)
- Argentina: 5% do volume total (Buenos Aires)

**Regras de negócio simuladas:**
- Sazonalidade: pico de +30% nas vendas no dia 24/12 (Natal)
- Vendas geradas apenas em dias úteis, respeitando feriados nacionais dos 3 países (biblioteca `holidays`)
- Cada venda só pode ser atribuída ao representante vinculado ao centro de distribuição correspondente (ou ao Gerente do país)

---

## 🏗️ Arquitetura

Arquitetura Medallion (Raw → Bronze → Silver → Gold), com uma camada Raw temporária como diferencial de design.

```
[Simulador de Dados - Faker]
        │  (gera arquivos JSON)
        ▼
[Volume Unity Catalog — Landing Zone]  (simula um Blob/ADLS externo)
        │  (Autoloader, batch diário)
        ▼
┌─────────────────────────────────────────────┐
│  RAW      → dado bruto, fiel à origem         │
│             TTL de 48h (expurgo automático)   │
├─────────────────────────────────────────────┤
│  BRONZE   → cópia fiel da Raw, sem casting/    │
│             transformação + metadados de       │
│             controle (retenção permanente)     │
├─────────────────────────────────────────────┤
│  SILVER   → dimensões SCD Tipo 2 (MERGE real) +│
│             fato Vendas (casting, câmbio,       │
│             cálculo de totais, padronização)   │
├─────────────────────────────────────────────┤
│  GOLD     → 3 tabelas de métricas, em inglês,  │
│             para consumo executivo             │
└─────────────────────────────────────────────┘
```

Todas as camadas utilizam **Delta Lake** como formato de armazenamento, viabilizando MERGE (SCD2), transações ACID e VACUUM (expurgo da Raw).

### Por que uma camada Raw com TTL de 48h?

Simula um padrão comum em ambientes corporativos regulados, onde a zona de pouso bruta (antes de qualquer tratamento) não deve reter dados por muito tempo — reduzindo custo de armazenamento e superfície de exposição de dados não tratados. A Bronze, por sua vez, é a camada de retenção permanente e fiel à origem.

### Por que a Bronze não faz casting/transformação?

Diferente de uma definição mais comum (Bronze com schema já aplicado), este projeto define a Bronze como uma **cópia fiel e imutável** da Raw, sem nenhuma transformação de conteúdo — apenas metadados de controle são adicionados. Todo casting de tipo, padronização de texto e cálculos são centralizados exclusivamente na Silver, reforçando a Bronze como "fonte da verdade" bruta para fins de auditoria. Detalhamento completo em [`docs/arquitetura.md`](docs/arquitetura.md).

---

## 🗂️ Modelagem de dados

### Dimensões (SCD Tipo 2 real, via MERGE)
- **dim_produtos** — 6 SKUs (3 produtos × 2 tamanhos), com nomes traduzidos em 4 idiomas (pt-BR, es-AR, es-MX, en) e preços multi-moeda
- **dim_lojas** — 6 centros de distribuição, com peso de distribuição de vendas
- **dim_representantes** — 9 representantes (3 Gerentes + 6 Supervisores)

O uso de SCD Tipo 2 permite reconstruir o contexto histórico de uma venda (ex: qual centro um representante pertencia no momento da transação). Todas as dimensões são padronizadas na Silver (maiúsculas, sem acento), servindo tanto a consumidores analíticos quanto de engenharia.

### Fato
- **fato_vendas** — grão: 1 linha = 1 item vendido por transação, com casting de tipos, cálculo de `valor_total_moeda_local`, e conversão para USD via JOIN efetivo por data com `dim_cambio` e as dimensões SCD2

### Câmbio
- **dim_cambio** — histórico diário de cotações (BRL, ARS, MXN → USD), com granularidade de uma cotação por dia (não intraday — reflete práticas reais de referência cambial, como a taxa PTAX do BCB)
- **Fonte primária:** API pública [exchangerate-api.com](https://www.exchangerate-api.com/) — conectividade testada e validada no Databricks Free Edition
- **Estratégia de resiliência (fallback):** se a API falhar, o pipeline reaproveita a última cotação salva com sucesso, marcando o registro como desatualizado, em vez de interromper a execução

---

## 📊 Camada Gold — KPIs de negócio

Três tabelas, todas em inglês (valores e nomes de colunas), pensadas para consumo executivo internacional:

| Tabela | Granularidade | Uso |
|---|---|---|
| `gold.sales_by_country` | País + dia | Resultado de cada país, em moeda local **e** USD |
| `gold.sales_global` | Dia, consolidado | Total global da operação, somente em USD |
| `gold.sales_by_product` | Produto + tamanho + país + dia | Performance de vendas por SKU (unidades e receita) |

Todas usam estratégia de **overwrite completo** a cada execução, recalculando a partir da Silver — garantindo que a Gold seja sempre um reflexo fiel e atual dos dados tratados.

---

## 🔐 Governança e Segurança

### Controle de acesso (RBAC) — modelo de referência
Um modelo de RBAC com 4 papéis (Arquitetura, Engenharia de Dados, Gestão, Analytics/BI) foi especificado e documentado em PySpark (`spark.sql("GRANT ...")`), como referência de como seria implementado em um ambiente Premium/Enterprise. **Não executável no Databricks Free Edition**, por ausência de acesso ao Account Console (necessário para criar grupos/identidades) — limitação confirmada na documentação oficial e registrada com transparência em [`docs/arquitetura.md`](docs/arquitetura.md).

### Relatório de governança de tags — funcional e executado
Diferente do RBAC, a tagueação de objetos **funciona plenamente** no Free Edition. O notebook [`src/governance/relatorio_governanca.py`](src/governance/relatorio_governanca.py):
- Aplica uma tag `camada` em cada schema (landing/raw/bronze/silver/gold), identificando a etapa Medallion
- Consulta tags reais via `information_schema` (catalog_tags, schema_tags, table_tags)
- Consolida tudo em uma tabela de relatório (`gold.governance_tags_report`)

---

## ⚙️ Stack técnica

| Componente | Tecnologia |
|---|---|
| Processamento | PySpark (Structured Streaming + batch) |
| Armazenamento | Delta Lake |
| Catálogo/Governança | Unity Catalog (schemas, tags, information_schema) |
| Geração de dados sintéticos | Faker |
| Calendário de feriados | biblioteca `holidays` |
| Ingestão incremental | Databricks Autoloader + streaming sobre Delta |
| Orquestração | Databricks Workflows (16 tasks) |
| Versionamento | GitHub + Databricks Git folders |
| Ambiente | Databricks Free Edition |

---

## 🔄 Pipeline de orquestração

Job diário, agendado às **06:00**, com **16 tasks** encadeadas por dependência (evoluído do plano inicial de 6 tasks conceituais, conforme cada camada foi modularizada em múltiplos notebooks — detalhes em [`docs/arquitetura.md`](docs/arquitetura.md)):

```
gerar_vendas_brasil ─┐
gerar_vendas_argentina ─┼─► ingestao_raw_dimensoes ─► bronze_dimensoes ─┬─► expurgo_raw (paralelo)
gerar_vendas_mexico ─┘       ingestao_raw_vendas ─► bronze_vendas ─────┘
                                                                        │
                             bronze_dimensoes ─► silver_produtos ──────┤
                                             ─► silver_lojas ──────────┤
                                             ─► silver_representantes ─┤
                       bronze_dimensoes + bronze_vendas ─► silver_cambio ┤
                                                  silver_fato_vendas (depende das 4 acima)
                                                            │
                             silver_fato_vendas ─► gold_sales_by_country
                                                ─► gold_sales_global
                                                ─► gold_sales_by_product
```

A definição completa (YAML) está versionada em [`workflows/pipeline_diario.yml`](workflows/pipeline_diario.yml).

Alertas de falha configurados para e-mail pessoal e e-mail simulando um grupo de trabalho.

---

## 📁 Estrutura do repositório

```
poc-lakehouse-food-latam/
├── src/
│   ├── simulador/       # Geração de dados sintéticos (Faker, holidays)
│   ├── raw/             # Ingestão via Autoloader + expurgo 48h
│   ├── bronze/          # Cópia fiel da Raw
│   ├── silver/          # SCD2 real (MERGE), câmbio, fato Vendas
│   ├── gold/             # 3 tabelas de métricas de negócio
│   ├── governance/      # Relatório de governança de tags
│   └── utils/           # Código reutilizável (FakerHelper, SCD2Handler)
├── workflows/           # Definição YAML do Workflow (16 tasks)
├── docs/
│   ├── arquitetura.md   # Decisões técnicas detalhadas + lições aprendidas
│   └── evidencias/      # Evidências de simulação de custos
└── README.md
```

---

## 💰 Estimativa de custos (simulação)

> O projeto roda integralmente no **Databricks Free Edition**, sem cloud provider fixo e com custo real **zero**. As simulações abaixo são um exercício hipotético de FinOps: "quanto este mesmo pipeline custaria se fosse implantado em produção, sobre Databricks com AWS ou sobre Azure Databricks?"

### Premissas da simulação

| Parâmetro | Valor |
|---|---|
| Tier | Premium |
| Tipo de compute | Lakeflow Jobs Serverless |
| Volume de dados | ~13.500 linhas/dia (Brasil + México + Argentina) |
| Execução mensal estimada | ~15 horas/mês (60 DBUs) |
| Região | Brasil (SA/Brazil South) |

### Simulação — Databricks sobre AWS
**Resultado: US$ 27,60/mês** (custo isolado de DBU — Databricks e infraestrutura em faturas separadas)
📄 [`docs/evidencias/Estimativa de custos AWS databricks.pdf`](<docs/evidencias/Estimativa de custos AWS databricks.pdf>)

### Simulação — Azure Databricks
**Resultado: US$ 9,09/mês** (custo consolidado — Databricks + infraestrutura Azure em uma única fatura)
📄 [`docs/evidencias/Estimativa de custos Azure databricks.pdf`](<docs/evidencias/Estimativa de custos Azure databricks.pdf>) | [`docs/evidencias/ExportedEstimate.xlsx`](docs/evidencias/ExportedEstimate.xlsx)

A diferença reflete como cada provedor estrutura o billing do Databricks, não uma nuvem sendo "mais barata" de forma absoluta — detalhamento em [`docs/arquitetura.md`](docs/arquitetura.md).

---

## 🚀 Como executar

1. **Pré-requisitos:** Databricks Free Edition (ou superior), GitHub, conta na API [exchangerate-api.com](https://www.exchangerate-api.com/) (gratuita, sem chave)
2. **Clonar o projeto:** conectar uma Databricks Git folder ao seu fork/clone deste repositório
3. **Infraestrutura:** criar o catalog `poc_latam_food` com os schemas `landing` (+ volume `blob_simulado`), `raw`, `bronze`, `silver`, `gold`
4. **Carga inicial das dimensões:** rodar manualmente, uma vez, `src/simulador/gerar_produtos.py`, `gerar_lojas.py`, `gerar_representantes.py`
5. **Importar o Workflow:** recriar o Job a partir de [`workflows/pipeline_diario.yml`](workflows/pipeline_diario.yml) (ou usar como referência para criar manualmente as 16 tasks) — agendado para rodar diariamente às 06:00
6. **Rodar manualmente ("Run now")** para validar a primeira execução ponta a ponta antes de confiar no agendamento automático

---

## 📚 Lições aprendidas (desafios reais encontrados e corrigidos)

Este projeto passou por um ciclo real de desenvolvimento iterativo — não apenas "construir e funcionar de primeira". Alguns problemas técnicos genuínos foram identificados, diagnosticados e corrigidos, todos documentados em detalhe em [`docs/arquitetura.md`](docs/arquitetura.md):

- **Streaming incremental sobre fonte com DELETE:** o TTL de 48h da Raw quebrou a leitura streaming da Bronze (`DELTA_SOURCE_IGNORE_DELETE`) — corrigido com `ignoreDeletes`, entendendo por que é seguro nesse caso específico
- **Race condition entre dois jobs agendados:** manter um job auxiliar temporário ativo simultaneamente ao Workflow definitivo causou duplicação real de dados (proteção `check-then-write` não é atômica) — corrigido descomissionando a solução temporária, com a causa raiz totalmente diagnosticada e documentada
- **TTL por data de ingestão vs. data de negócio:** um teste real expôs que o expurgo de 48h opera sobre quando o dado *chegou* na Raw, não sobre a data do evento de negócio — comportamento correto, mas que exigiu investigação para confirmar
- **Correção de ordem de arquitetura:** o desenho inicial posicionava o expurgo da Raw *antes* da Bronze processar os dados — corrigido antes de qualquer perda real ocorrer

---

## 📌 Decisões de arquitetura

Documentadas em detalhe, incluindo todo o histórico de decisões e correções, em [`docs/arquitetura.md`](docs/arquitetura.md).
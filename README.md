# POC Lakehouse — Food LATAM

Projeto de portfólio em Engenharia de Dados que simula a plataforma de dados de uma empresa de alimentos com operação na América Latina (Brasil, Argentina e México). O objetivo é demonstrar, de ponta a ponta, um pipeline de dados profissional: da geração de dados sintéticos até a disponibilização de métricas de negócio, seguindo padrões reais de mercado.

> **Status:** em construção 🚧

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

**Sazonalidade:** pico de +30% nas vendas no dia 24/12 (Natal).

---

## 🏗️ Arquitetura

Arquitetura Medallion (Raw → Bronze → Silver → Gold), com uma camada Raw temporária como diferencial de design.

```
[Simulador de Dados]
        │  (gera arquivos JSON)
        ▼
[Volume Unity Catalog — Landing Zone]  (simula um Blob/ADLS externo)
        │  (Autoloader, batch diário)
        ▼
┌─────────────────────────────────────────────┐
│  RAW      → dado bruto, fiel à origem         │
│             TTL de 48h (expurgo automático)   │
├─────────────────────────────────────────────┤
│  BRONZE   → schema aplicado + metadados       │
│             de controle (data_ingestao, etc.) │
├─────────────────────────────────────────────┤
│  SILVER   → dimensões SCD Tipo 2 +            │
│             fato Vendas (grão: item vendido)  │
├─────────────────────────────────────────────┤
│  GOLD     → métricas e KPIs de negócio        │
│             (em definição)                    │
└─────────────────────────────────────────────┘
```

Todas as camadas utilizam **Delta Lake** como formato de armazenamento, viabilizando MERGE (SCD2), transações ACID e VACUUM (expurgo da Raw).

### Por que uma camada Raw com TTL de 48h?

Simula um padrão comum em ambientes corporativos regulados, onde a zona de pouso bruta (antes de qualquer tratamento) não deve reter dados por muito tempo — reduzindo custo de armazenamento e superfície de exposição de dados não tratados, enquanto a Bronze já garante retenção de longo prazo com schema aplicado.

---

## 🗂️ Modelagem de dados

### Dimensões (SCD Tipo 2)
- **dim_produtos** — 6 SKUs (3 produtos × 2 tamanhos)
- **dim_lojas** — 6 centros de distribuição
- **dim_representantes** — 9 representantes (3 Gerentes + 6 Supervisores)

O uso de SCD Tipo 2 permite reconstruir o contexto histórico de uma venda (ex: qual região um representante pertencia no momento da transação), refletindo uma prática comum em empresas reais do setor.

### Fato
- **fato_vendas** — grão: 1 linha = 1 item vendido por transação (produto, quantidade, valor, país, centro, representante, data)

### Câmbio
- **dim_cambio** — tabela de apoio para conversão de moeda local (BRL, ARS, MXN) para USD (fonte: API externa em avaliação de conectividade, com fallback de tabela própria)

---

## ⚙️ Stack técnica

| Componente | Tecnologia |
|---|---|
| Processamento | PySpark |
| Armazenamento | Delta Lake |
| Catálogo/Governança | Unity Catalog |
| Geração de dados sintéticos | Faker |
| Ingestão incremental | Databricks Autoloader (batch diário) |
| Orquestração | Databricks Workflows |
| Versionamento | GitHub + Databricks Git folders |
| Ambiente | Databricks Free Edition |

---

## 🔄 Pipeline de orquestração

Job diário (06:00), com 6 tasks encadeadas:

1. **Simulador** — gera arquivos JSON na Landing Zone (Volume)
2. **Raw** — Autoloader lê a Landing Zone e grava na camada Raw
3. **Expurgo Raw** — remove registros com mais de 48h (VACUUM/DELETE)
4. **Bronze** — aplica schema e metadados de controle
5. **Silver** — MERGE SCD2 nas dimensões + carga incremental da fato Vendas
6. **Gold** — cálculo de métricas de negócio

Alertas de falha configurados para e-mail pessoal e e-mail simulando um grupo de trabalho (ex: `data-eng-alerts@empresa-fake.com`).

---

## 📁 Estrutura do repositório

```
poc-lakehouse-food-latam/
├── src/
│   ├── simulador/       # Geração de dados sintéticos (Faker)
│   ├── raw/             # Ingestão via Autoloader + expurgo 48h
│   ├── bronze/          # Aplicação de schema
│   ├── silver/          # SCD2 (dimensões) + fato Vendas
│   ├── gold/            # Métricas e KPIs de negócio
│   └── utils/           # Código reutilizável (classes, schemas, conexões)
├── workflows/           # Definição de orquestração (Databricks Workflows / Asset Bundles)
├── docs/                # Documentação complementar (arquitetura, decisões)
└── README.md
```

---

## 💰 Estimativa de custos (simulação)

> O projeto roda integralmente no **Databricks Free Edition**, sem cloud provider fixo e com custo real **zero**. As simulações abaixo são um exercício hipotético de FinOps: "quanto este mesmo pipeline custaria se fosse implantado em produção, sobre Databricks com AWS ou sobre Azure Databricks?" — não indicam onde o projeto está de fato hospedado, apenas comparam como o billing se comporta em cada cloud provider.

### Premissas da simulação

| Parâmetro | Valor |
|---|---|
| Tier | Premium |
| Tipo de compute | Lakeflow Jobs Serverless |
| Volume de dados | ~13.500 linhas/dia (Brasil + México + Argentina) |
| Execução diária estimada | ~30 minutos (6 tasks encadeadas) |
| Execução mensal estimada | ~15 horas/mês (60 DBUs) |
| Região | Brasil (SA/Brazil South) |

### Simulação — Databricks sobre AWS

Calculadora oficial da Databricks ([databricks.com/product/pricing](https://www.databricks.com/product/pricing/product-pricing/instance-types)): `Premium` + `Lakeflow Jobs Serverless` + `SA (Brazil) — $0,46/DBU` + `60 DBUs`.

![Simulação de custos - Databricks AWS](../docs/simulacao_custos_databricks_aws.png)

**Resultado: US$ 27,60/mês** (custo isolado de DBU; neste modelo, Databricks e infraestrutura de nuvem são cobradas em faturas separadas)

📄 Evidência completa: [`docs/evidencias/databricks_aws_estimate.pdf`](docs/evidencias/databricks_aws_estimate.pdf)

### Simulação — Azure Databricks

Calculadora do Azure ([azure.microsoft.com/pricing/calculator](https://azure.microsoft.com/en-us/pricing/calculator/)): `Jobs Compute Workload` + `Premium Tier` + `Brazil South` + `1x D4s v5 (4 vCPUs, 16 GB RAM)` + `15 horas/mês`.

![Simulação de custos - Azure Databricks](../docs/simulacao_custos_azure_databricks.png)

**Resultado: US$ 9,09/mês** (custo consolidado — Databricks + infraestrutura Azure em uma única fatura)

📄 Evidência completa: [`docs/evidencias/azure_databricks_estimate.xlsx`](docs/evidencias/azure_databricks_estimate.xlsx)

### Por que os valores são diferentes entre AWS e Azure?

A diferença (US$ 27,60 vs US$ 9,09) não significa que uma nuvem é "mais barata" de forma absoluta — reflete como cada provedor estrutura o billing do Databricks:

- **AWS**: a calculadora da própria Databricks estima **apenas o custo de DBU**, sem incluir a VM de infraestrutura (que seria cobrada separadamente, direto na conta AWS)
- **Azure**: a Databricks é um serviço de primeira parte da Microsoft, então a calculadora do Azure já apresenta o custo **consolidado** (DBU + VM) em uma única fatura

Esse tipo de comparação evidencia domínio de um ponto real de mercado: entender como o modelo de billing muda conforme o cloud provider é uma habilidade de FinOps aplicada à Engenharia de Dados.

---

## 📊 Camada Gold — KPIs de negócio

*(em definição)*

---

## 🚀 Como executar

*(seção a ser preenchida conforme o pipeline for implementado)*

---

## 📌 Decisões de arquitetura

Documentadas em detalhe em [`docs/arquitetura.md`](docs/arquitetura.md).
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

## 5. Modelagem: grão da fato Vendas

O grão definido é: **1 linha = 1 item vendido por transação** (produto+tamanho, quantidade, valor, país, centro, representante, data). Essa granularidade permite qualquer agregação futura na camada Gold (por produto, por região, por representante, por período) sem perda de detalhe.

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
| Gold | A definir | Depende das métricas/agregações que serão construídas |

---

## 9. Orquestração

Job diário, agendado às 06:00, com 6 tasks encadeadas (dependência sequencial):

1. **Simulador** — gera arquivos JSON na Landing Zone
2. **Raw** — Autoloader lê a Landing Zone e grava na Raw
3. **Expurgo Raw** — remove registros com mais de 48h
4. **Bronze** — aplica schema e metadados de controle
5. **Silver** — MERGE SCD2 nas dimensões + carga incremental da fato Vendas
6. **Gold** — cálculo de métricas de negócio

Alertas de falha configurados para dois destinatários (e-mail pessoal + e-mail simulando um grupo de trabalho), reforçando uma prática de observabilidade mínima esperada em pipelines de produção.

---

## 10. Decisões técnicas complementares (a incorporar durante o desenvolvimento)

- **Databricks Widgets**: parametrização de notebooks (ex: `pais` como parâmetro, permitindo reuso do mesmo notebook para Brasil, Argentina e México em vez de 3 notebooks duplicados)
- **Programação Orientada a Objetos**: lógica reutilizável centralizada em `src/utils` (ex: uma classe `SCD2Handler` usada pelos 3 notebooks de dimensão)
- **Infrastructure as Code (IaC)**: avaliação do uso de Databricks Asset Bundles para definir o Workflow como código versionado, em vez de configuração manual pela interface

---

## 11. Estimativa de custos

Ver seção específica no [README](../README.md#-estimativa-de-custos-simulação), com simulações comparativas entre Databricks sobre AWS e Azure Databricks, incluindo evidências em [`docs/evidencias/`](evidencias/).
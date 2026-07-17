from datetime import date
from pyspark.sql import DataFrame
from pyspark.sql.functions import lit


class SCD2Handler:
    """
    Centraliza a lógica de controle SCD Tipo 2 para as dimensões
    do projeto (Produtos, Lojas, Representantes).

    Nesta primeira etapa (carga inicial), a classe apenas prepara
    um DataFrame "cru" adicionando as colunas de controle SCD2.
    A lógica de MERGE (fechar versão antiga / abrir nova) será
    incorporada quando simularmos a primeira mudança de atributo.
    """

    def __init__(self, data_referencia: date = None):
        # Permite injetar uma data fixa (útil em testes);
        # por padrão, usa a data de execução do notebook.
        self.data_referencia = data_referencia or date.today()

    def iniciar_controle_scd2(self, df: DataFrame) -> DataFrame:
        """
        Adiciona as colunas de controle SCD2 a um DataFrame novo:
        - data_inicio: data em que esta versão do registro passou a valer
        - data_fim: nulo, pois é a versão vigente
        - flag_ativo: True, pois é a versão vigente
        """
        return (
            df.withColumn("data_inicio", lit(self.data_referencia))
              .withColumn("data_fim", lit(None).cast("date"))
              .withColumn("flag_ativo", lit(True))
        )
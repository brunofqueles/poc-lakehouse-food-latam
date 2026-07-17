import random
from faker import Faker


class FakerHelper:
    """
    Encapsula a geração de dados fake, ajustando automaticamente
    o idioma/locale do Faker de acordo com o país informado.
    Evita que cada notebook precise saber qual locale usar.

    Também simula pequenas inconsistências de qualidade de dados
    (comuns em fontes reais), para dar propósito real ao tratamento
    de dados na camada Silver.
    """

    # Mapeamento de país -> locale do Faker
    _LOCALES = {
        "brasil": "pt_BR",
        "argentina": "es_AR",
        "mexico": "es_MX",
    }

    def __init__(self, pais: str, probabilidade_sujeira: float = 0.15):
        pais_normalizado = pais.strip().lower()
        if pais_normalizado not in self._LOCALES:
            raise ValueError(
                f"País '{pais}' não suportado. Use: {list(self._LOCALES.keys())}"
            )
        self.pais = pais_normalizado
        self.locale = self._LOCALES[pais_normalizado]
        self.faker = Faker(self.locale)
        self.probabilidade_sujeira = probabilidade_sujeira

    def gerar_nome(self) -> str:
        """Gera um nome completo coerente com o país configurado."""
        return self.aplicar_inconsistencia(self.faker.name())

    def gerar_cidade(self) -> str:
        """Gera um nome de cidade coerente com o país configurado."""
        return self.aplicar_inconsistencia(self.faker.city())

    def gerar_email(self, nome: str) -> str:
        """Gera um e-mail simples a partir de um nome já gerado."""
        return self.faker.email()

    def gerar_telefone(self) -> str:
        """Gera um telefone coerente com o país configurado."""
        return self.faker.phone_number()

    def aplicar_inconsistencia(self, texto: str) -> str:
        """
        Simula pequenas inconsistências de qualidade de dados,
        comuns em fontes reais de produção. Com probabilidade
        configurável (padrão 15%), aplica uma transformação:
        - CAIXA ALTA
        - Espaços extras no início/fim
        Na maioria das vezes (85% padrão), retorna o texto normal.
        """
        if random.random() > self.probabilidade_sujeira:
            return texto

        tipo_sujeira = random.choice(["maiuscula", "espacos_extras"])

        if tipo_sujeira == "maiuscula":
            return texto.upper()
        elif tipo_sujeira == "espacos_extras":
            return f"  {texto}  "

        return texto
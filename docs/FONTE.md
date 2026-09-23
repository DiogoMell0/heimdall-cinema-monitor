# Fonte da programação

O coletor usa a API de Conteúdo do Ingresso.com:

```text
https://api-content.ingresso.com/v0/sessions/city/{cidade}/event/{filme}
```

Na investigação de setembro de 2026, essa rota retornou a programação de várias datas em uma chamada. A resposta já trazia cinema, sala, horário, idioma, formato e link de compra. Isso permitiu consultar por HTTP e aplicar os filtros localmente.

A correspondência foi conferida com 39 sessões na página do filme. Os [registros históricos](../examples/historico/README.md) permitem repetir essa comparação.

## Cuidados com o retorno

Uma data ausente continua pendente para a próxima consulta. Um erro HTTP 404, por outro lado, é tratado como falha de coleta: pode indicar um identificador inválido, e não necessariamente ausência de sessões.

Os filtros usam os nomes das características. Na captura original, Infinity Vision e Dublado tinham o mesmo ID numérico de tipo, por isso esse ID não servia para diferenciá-los.

A [documentação de integração](https://suporte.ingresso.com/portal/pt-br/kb/articles/integra%C3%A7%C3%A3o-com-a-api-de-conte%C3%BAdo-1-11-2022) menciona código de parceiro. A rota usada respondeu sem esse parâmetro durante as validações. Esse acesso pode mudar; uma recusa ou mudança de estrutura exige revisão do coletor.

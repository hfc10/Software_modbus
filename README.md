# Painel Modbus — Secador de Ar Regenerativo

Software de supervisão (IHM) do secador de ar de sílica-gel. Roda num PC ou mini-PC com tela,
conversa com o ESP32-S3 do secador por **Modbus RTU via RS-485** e mostra tudo num painel web
local: leituras ao vivo, estado de cada estágio, alarmes, eventos, histórico e comandos.

O controle mora no ESP32, não aqui. Se o painel fechar ou o cabo soltar, o secador continua
secando sozinho; o painel só mostra e comanda.

Firmware do ESP32: [hfc10/ProjetoWifiSecador2026](https://github.com/hfc10/ProjetoWifiSecador2026)
(arquivo `src/teste_modbus.cpp`).

---

## O que o painel mostra

- **Processo:** desenho do tubo com os dois estágios, a saturação da sílica de cada um, a
  resistência acesa quando aquece e a ventoinha girando.
- **Estágios:** umidade da base, estado ("aquecendo ha 4min", "ocioso ha 1h05"), aviso de
  **ciclo pendente** e botão da resistência.
- **Controle:** troca entre automático e manual, Ventoinha 2 e os limites de controle
  (umidade para ligar, umidade para desligar, temperatura máxima do manual), gravados no ESP.
  No automático os botões de saída ficam bloqueados, porque o ESP é quem decide.
- **Sensores:** um cartão por sensor com valor atual e mini-gráfico. Clicar abre o **gráfico
  grande** com o histórico de 1 h, 6 h, 24 h ou 7 dias.
- **Alarmes:** falha de funcionamento e sílica no fim da vida útil, com aviso na tela.
- **Ciclos da sílica:** contador de regenerações concluídas.
- **Eventos recentes:** resistência ligou/desligou, troca de modo, mudança de limites, alarmes,
  sensor sem leitura e conexão perdida, gravados com data e hora.
- **Conexão perdida:** faixa vermelha no topo quando o ESP para de responder.

---

## Instalação

Requisitos: **Python 3.10 ou mais novo** (desenvolvido no 3.13) e um adaptador **USB ↔ RS-485**.

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

No **Gerenciador de Dispositivos** do Windows, veja em qual porta COM o adaptador aparece e
coloque em `config.json`.

## Como rodar

```
venv\Scripts\python.exe app.py
```

Abra **http://localhost:5000** no navegador.

### Sem o ESP32 (simulação)

```
venv\Scripts\python.exe app.py --simular
```

Gera dados fictícios com a mesma lógica do firmware: aquecimento, corte a 85 °C, retomada com
prioridade, ciclos concluídos. Os botões funcionam; um comando manual vale por 90 s e depois a
simulação volta ao automático. A porta serial não é aberta, e o histórico vai para um banco
separado (`historico_simulado.db`).

### Opções de linha de comando

| Opção | O que faz |
|---|---|
| `--simular` | Roda sem o ESP32, com dados simulados |
| `--porta-http N` | Usa outra porta para o painel (padrão: a do `config.json`) |
| `--gravacao-seg N` | Grava o histórico em disco a cada N segundos (útil para testar) |

---

## Configuração (`config.json`)

Todos os campos são opcionais; o que faltar usa o padrão.

| Campo | Padrão | Significado |
|---|---|---|
| `porta_serial` | `COM6` | Porta do adaptador USB ↔ RS-485 |
| `baud_rate` | `9600` | Velocidade da serial (igual ao firmware) |
| `id_escravo` | `1` | Endereço Modbus do ESP32 |
| `porta_http` | `5000` | Porta do painel no navegador |
| `intervalo_leitura_seg` | `2` | De quanto em quanto tempo lê o ESP |
| `intervalo_gravacao_seg` | `10` | De quanto em quanto tempo grava o histórico em disco |
| `retencao_dias` | `30` | Quantos dias de histórico manter |

---

## Uso contínuo (mini-PC com tela)

- **`iniciar_painel.bat`** sobe o painel e o reinicia sozinho se ele cair. Para abrir junto com o
  Windows: `Win+R`, digite `shell:startup` e coloque um atalho para ele na pasta que abrir.
  Argumentos passam direto para o `app.py` (ex.: `iniciar_painel.bat --simular`).
- **`abrir_quiosque.bat`** abre o Edge em tela cheia, já no modo toque (`Alt+F4` sai). Se mudar a
  `porta_http`, ajuste a porta dentro do arquivo.
- **Modo toque:** botões e textos maiores, para tela de toque a distância. Liga pelo botão no
  topo ou abrindo `http://localhost:5000/?quiosque=1`.
- O servidor de produção (**waitress**) é usado automaticamente. Sem ele, o painel cai para o
  servidor de desenvolvimento do Flask.
- Se o ESP parar de responder por 3 leituras seguidas, a porta serial é fechada e reaberta
  sozinha (cobre o adaptador USB desplugado e plugado de novo).

---

## Histórico e exportação

- As leituras vão para **`historico.db`** (SQLite, ao lado do `app.py`) e ficam
  `retencao_dias` dias. Os mini-gráficos continuam depois de reiniciar o painel.
- Os eventos ficam no mesmo banco.
- **Exportar CSV:** link no rodapé do painel (últimas 24 h), ou
  `http://localhost:5000/api/exportar.csv?horas=N`.

## API

O painel conversa com o próprio servidor por estas rotas, que também servem para integrar com
outros sistemas:

| Rota | Função |
|---|---|
| `GET /api/dados` | Estado completo em JSON (sensores, saídas, modo, limites, alarmes, eventos) |
| `GET /api/historico?sensor=N&horas=H` | Série de um sensor para o gráfico grande |
| `GET /api/exportar.csv?horas=H` | Leituras em CSV |
| `POST /api/modo` | `{"manual": true}` |
| `POST /api/ventoinha` | `{"ligar": true}`; com `"id": 2` comanda a Ventoinha 2 |
| `POST /api/resistencia` | `{"estagio": 1, "ligar": true}` |
| `POST /api/limites` | `{"ligar": 55, "desligar": 8, "temp": 50}` |

Comandos de saída só têm efeito com o ESP em modo manual.

---

## Mapa de registradores

Precisa bater com o firmware (`src/teste_modbus.cpp`). Se o mapa mudar lá, atualize a lista
`SENSORES` e as constantes `REG_*` no `app.py`.

**Input Registers (0x04)**, valores ×10

| Endereço | Conteúdo |
|---|---|
| 0–1 | Estágio 1 Topo: temperatura, umidade |
| 2–3 | Estágio 1 Base: temperatura, umidade |
| 4–5 | Estágio 2 Topo: temperatura, umidade |
| 6–7 | Estágio 2 Base: temperatura, umidade |
| 8–9 | Ambiente BMP180: temperatura, pressão (hPa) |
| 10–11 | SHT25 no canal 0 (temporário, de teste) |
| 12–13 | Segundos desde que o Estágio 1 / 2 ligou ou desligou (sem ×10; satura em 65535) |

**Discrete Inputs (0x02)**

| Endereço | Conteúdo |
|---|---|
| 0–5 | Sensor respondeu na última leitura (mesma ordem acima) |
| 6 | Alarme: falha de funcionamento |
| 7 | Alarme: sílica no fim da vida útil |
| 8–9 | Estágio 1 / 2 com ciclo pendente |

**Holding Registers (0x03 / 0x10)**

| Endereço | Conteúdo |
|---|---|
| 1 | Ciclos de regeneração concluídos |
| 2 | Limite de umidade do topo para ligar (×10) |
| 3 | Limite de umidade da base para desligar (×10) |
| 4 | Temperatura máxima do modo manual (×10) |

Os limites 2–4 são gravados juntos, num quadro só: o ESP valida o conjunto e recusa valores
inválidos.

**Coils (0x01 / 0x05)**

| Endereço | Conteúdo |
|---|---|
| 0 | Ventoinha 1 |
| 1 | Resistência Estágio 1 |
| 2 | Resistência Estágio 2 |
| 3 | Ventoinha 2 (só manual) |
| 4 | Modo manual (1) / automático (0) |

---

## Arquivos

```
app.py               servidor: leitura Modbus, simulação, histórico, eventos e API
templates/index.html o painel (HTML, CSS e JavaScript num arquivo só, sem dependências)
config.json          porta serial e demais ajustes
requirements.txt     dependências Python
iniciar_painel.bat   sobe o painel e reinicia se cair
abrir_quiosque.bat   abre o navegador em tela cheia no modo toque
```

O histórico (`historico.db`, `historico_simulado.db`) e o ambiente `venv/` ficam fora do Git.

## Tecnologias

- **[Flask](https://flask.palletsprojects.com/)** — servidor web e API.
- **[pymodbus](https://pymodbus.readthedocs.io/)** + **pyserial** — mestre Modbus RTU pela serial.
- **[waitress](https://docs.pylonsproject.org/projects/waitress/)** — servidor de produção.
- **SQLite** (já vem no Python) — histórico e eventos.
- Painel em HTML, CSS e JavaScript puros, com gráficos desenhados em canvas. Só as fontes vêm
  do Google Fonts; sem internet, o navegador usa as fontes do sistema.

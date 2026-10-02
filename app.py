"""
Painel Modbus RTU - Secador de Ar

Le os registradores do ESP32 (escravo Modbus, ligado via adaptador USB->RS-485) e serve um
dashboard web local, no mesmo estilo visual da pagina que roda no proprio ESP32 via WiFi - so
que aqui os dados vem pela serial (RS-485), nao pela rede.

Mapa de registradores (tem que bater com o firmware, em src/teste_modbus.cpp do projeto do ESP32):
  Input Registers (0x04), valores x10:
    0-1   SHT40 E1-Topo   (temperatura, umidade)
    2-3   SHT40 E1-Base   (temperatura, umidade)
    4-5   SHT40 E2-Topo   (temperatura, umidade)
    6-7   SHT40 E2-Base   (temperatura, umidade)
    8-9   BMP180 Ambiente (temperatura, pressao em hPa)
    10-11 SHT25 (canal 0) (temperatura, umidade) - TEMPORARIO
  Discrete Inputs (0x02):
    0-5 sensor ok (mesma ordem dos SENSORES abaixo)
    6   ALARME: falha de funcionamento
    7   ALARME: saturacao/fim de vida da silica-gel
    8-9 Estagio 1 / 2 com ciclo incompleto (cortado por temperatura, aguardando retomar)
  Holding Registers (0x03/0x06):
    1  ciclos de regeneracao da silica-gel ja realizados
    2  limite de umidade do topo pra ligar (x10)
    3  limite de umidade da base pra desligar (x10)
    4  temperatura maxima do modo manual (x10)
  Coils (0x01):
    0  ventoinha 1 ligada/desligada
    1  resistencia Estagio 1 ligada/desligada
    2  resistencia Estagio 2 ligada/desligada
    3  ventoinha 2 ligada/desligada (so manual)
    4  modo manual (1) / automatico (0)
  No automatico o ESP decide e ignora escrita nos coils 0-3; no manual eles comandam as saidas.

Como usar:
  1) pip install -r requirements.txt
  2) Ajusta "porta_serial" no config.json (ao lado deste arquivo) pra bater com o adaptador
     USB->RS-485 (confere no Gerenciador de Dispositivos do Windows, ex: "COM6")
  3) python app.py
  4) Abre http://localhost:5000 no navegador

Sem o ESP32 conectado: python app.py --simular
  Gera dados simulados (secagem, aquecimento, corte de temperatura, prioridade de retomada) e
  os botoes do painel tambem funcionam. Nao abre a porta serial.

Outras opcoes de linha de comando (uteis pra testar sem mexer no config.json):
  --porta-http N     porta do painel (padrao: config.json, 5000)
  --gravacao-seg N   intervalo de gravacao do historico em disco (padrao: config.json, 10)

Uso continuo (mini-PC): iniciar_painel.bat sobe o painel e reinicia sozinho se cair;
abrir_quiosque.bat abre o navegador em tela cheia no modo toque.

Historico: as leituras sao gravadas num SQLite (historico.db, ao lado deste arquivo; no modo
simulacao usa historico_simulado.db) e ficam 30 dias. Os mini-graficos sobrevivem a reinicio,
/api/historico alimenta o grafico grande de cada sensor e /api/exportar.csv?horas=24 baixa as
leituras em CSV. Os eventos (resistencia ligou/desligou, alarmes, conexao) ficam na tabela
"eventos" do mesmo banco e aparecem na lista "Alarmes e eventos" do painel. O estado das
saidas (resistencias e ventoinha) e gravado junto com as leituras, na tabela "saidas", e
/api/tendencia usa isso pra marcar no grafico de tendencia quando cada estagio aqueceu.

PIN (opcional): com "pin_ajustes" no config.json, trocar o modo e gravar limites passam a pedir
esse PIN no painel (cabecalho X-Pin na API). Vazio = sem PIN.
"""

import collections
import contextlib
import csv
import datetime
import hmac
import io
import json
import os
import random
import sqlite3
import sys
import threading
import time

from flask import Flask, Response, jsonify, render_template, request
from pymodbus.client import ModbusSerialClient

PASTA_APP = os.path.dirname(os.path.abspath(__file__))
SIMULAR = "--simular" in sys.argv


# ==============================================================================
# Configuracao (config.json ao lado deste arquivo; tudo opcional)
# ==============================================================================
CONFIG_PADRAO = {
    "porta_serial": "COM6",
    "baud_rate": 9600,
    "id_escravo": 1,
    "porta_http": 5000,
    "intervalo_leitura_seg": 2,
    "intervalo_gravacao_seg": 10,
    "retencao_dias": 30,
    "pin_ajustes": "",
    # Referencias do firmware que o painel so mostra (anel de ciclos, termometro, linhas dos
    # graficos). Tem que bater com src/teste_modbus.cpp: o controle de verdade e do ESP.
    "ciclos_maximos_silica": 5000,
    "temp_corte_c": 85.0,
    "temp_religa_c": 70.0,
}


def carregar_config():
    config = dict(CONFIG_PADRAO)
    caminho = os.path.join(PASTA_APP, "config.json")
    try:
        with open(caminho, encoding="utf-8") as arquivo:
            lido = json.load(arquivo)
        for chave, valor in lido.items():
            # PIN escrito sem aspas (1234) tambem vale; sem isso o painel ficaria sem PIN
            if chave == "pin_ajustes" and type(valor) is int:
                valor = str(valor)
            # 85 vale onde o padrao e 85.0
            if type(CONFIG_PADRAO.get(chave)) is float and type(valor) is int:
                valor = float(valor)
            if chave not in CONFIG_PADRAO:
                print(f"config.json: chave desconhecida ignorada: {chave}")
            elif type(valor) is not type(CONFIG_PADRAO[chave]):
                print(f"config.json: '{chave}' com tipo errado, usando o padrao {CONFIG_PADRAO[chave]}")
            else:
                config[chave] = valor
    except FileNotFoundError:
        pass  # sem config.json: usa os padroes
    except (OSError, ValueError) as erro:
        print(f"config.json ilegivel ({erro}), usando os padroes")

    if config["ciclos_maximos_silica"] <= 0:
        print("config.json: 'ciclos_maximos_silica' precisa ser maior que zero, usando o padrao")
        config["ciclos_maximos_silica"] = CONFIG_PADRAO["ciclos_maximos_silica"]
    if not 0 < config["temp_religa_c"] < config["temp_corte_c"]:
        print("config.json: 'temp_religa_c' precisa ser menor que 'temp_corte_c', usando os padroes")
        config["temp_religa_c"] = CONFIG_PADRAO["temp_religa_c"]
        config["temp_corte_c"] = CONFIG_PADRAO["temp_corte_c"]
    return config


def argumento_inteiro(nome):
    """Valor de '--nome N' na linha de comando (ou None)."""
    if nome in sys.argv:
        try:
            return int(sys.argv[sys.argv.index(nome) + 1])
        except (IndexError, ValueError):
            print(f"{nome} precisa de um numero inteiro, ignorando")
    return None


CONFIG = carregar_config()
PORTA_SERIAL = CONFIG["porta_serial"]
BAUD_RATE = CONFIG["baud_rate"]
ID_ESCRAVO = CONFIG["id_escravo"]
PORTA_HTTP = argumento_inteiro("--porta-http") or CONFIG["porta_http"]
INTERVALO_LEITURA_SEG = CONFIG["intervalo_leitura_seg"]
INTERVALO_GRAVACAO_SEG = argumento_inteiro("--gravacao-seg") or CONFIG["intervalo_gravacao_seg"]
RETENCAO_DIAS = CONFIG["retencao_dias"]
PIN_AJUSTES = CONFIG["pin_ajustes"].strip()
CICLOS_MAXIMOS_SILICA = CONFIG["ciclos_maximos_silica"]
TEMP_CORTE = CONFIG["temp_corte_c"]
TEMP_RELIGA = CONFIG["temp_religa_c"]

# Mesma ordem/enderecos definidos no firmware (src/teste_modbus.cpp)
SENSORES = [
    {"nome": "SHT40 E1-Topo", "tipo": "SHT40", "reg_valor1": 0, "reg_valor2": 1, "reg_status": 0},
    {"nome": "SHT40 E1-Base", "tipo": "SHT40", "reg_valor1": 2, "reg_valor2": 3, "reg_status": 1},
    {"nome": "SHT40 E2-Topo", "tipo": "SHT40", "reg_valor1": 4, "reg_valor2": 5, "reg_status": 2},
    {"nome": "SHT40 E2-Base", "tipo": "SHT40", "reg_valor1": 6, "reg_valor2": 7, "reg_status": 3},
    {"nome": "BMP180 Ambiente", "tipo": "BMP180", "reg_valor1": 8, "reg_valor2": 9, "reg_status": 4},
    {"nome": "SHT25 (canal 0)", "tipo": "SHT2X", "reg_valor1": 10, "reg_valor2": 11, "reg_status": 5},
]
REG_COIL_VENTOINHA = 0
REG_COIL_RESISTENCIA_E1 = 1
REG_COIL_RESISTENCIA_E2 = 2
REG_COIL_VENTOINHA2 = 3
REG_COIL_MODO_MANUAL = 4
REG_ALARME_FALHA = 6
REG_ALARME_SATURACAO = 7
REG_CICLO_INCOMPLETO_E1 = 8  # Discrete Input 8 (E1) e 9 (E2)
REG_DESDE_MUDANCA_E1 = 12  # Input Register 12 (E1) e 13 (E2): segundos desde que ligou/desligou
REG_CICLOS_SILICA = 1
REG_LIMITE_LIGAR = 2
REG_LIMITE_DESLIGAR = 3
REG_TEMP_MAXIMA_MANUAL = 4

# Quantos pontos manter pro mini-grafico de tendencia de cada sensor (um ponto a cada
# INTERVALO_LEITURA_SEG, entao 30 pontos = 1 minuto de historico).
HISTORICO_TAMANHO = 30

# Eventos: quantos guardar em memoria e quantos mandar pro painel
EVENTOS_EM_MEMORIA = 50
EVENTOS_NO_PAINEL = 30

# Nomes dos alarmes como aparecem no reconhecimento (chave usada pela API e pelo painel)
ALARMES = {"falha": "falha de funcionamento", "saturacao": "sílica saturada / fim de vida"}

# Depois de quantas rodadas seguidas sem nenhuma resposta do ESP32 a serial e fechada e reaberta
# (cobre o adaptador USB-RS485 que foi desplugado e plugado de novo)
RODADAS_SEM_RESPOSTA_PRA_RECONECTAR = 3

ARQUIVO_HISTORICO = os.path.join(
    PASTA_APP, "historico_simulado.db" if SIMULAR else "historico.db"
)
CICLOS_ENTRE_LIMPEZAS = 1000  # ~33 min com leitura a cada 2s

app = Flask(__name__)
cliente = ModbusSerialClient(
    port=PORTA_SERIAL, baudrate=BAUD_RATE, bytesize=8, parity="N", stopbits=1, timeout=1
)
# O cliente serial nao e seguro pra uso simultaneo: a thread de leitura e os botoes do painel
# (que chegam em threads do servidor web) passam todos por esta trava.
trava_modbus = threading.Lock()

estado = {
    "conectado": False,
    "simulacao": SIMULAR,
    "sensores": [],
    "ventoinha": False,
    "ventoinha2": False,
    "modoManual": False,
    "limites": {"ligar": 0, "desligar": 0, "temp": 0},
    "cicloIncompleto": [False, False],
    "desdeSeg": [None, None],
    "resistenciaE1": False,
    "resistenciaE2": False,
    "alarmeFalha": False,
    "alarmeSaturacao": False,
    # reconhecido pelo operador no painel; volta a False quando o alarme normaliza
    "alarmesReconhecidos": {"falha": False, "saturacao": False},
    "pinAtivo": bool(PIN_AJUSTES),
    "referencias": {"ciclosMaximos": CICLOS_MAXIMOS_SILICA, "tempCorte": TEMP_CORTE, "tempReliga": TEMP_RELIGA},
    "ciclosSilica": 0,
    "ultimaAtualizacao": 0,
    "historico": [],
    "eventos": [],
}
trava = threading.Lock()

historico_valores = [
    {
        "valor1": collections.deque(maxlen=HISTORICO_TAMANHO),
        "valor2": collections.deque(maxlen=HISTORICO_TAMANHO),
    }
    for _ in SENSORES
]
eventos_recentes = collections.deque(maxlen=EVENTOS_EM_MEMORIA)  # o mais novo fica na direita


# ==============================================================================
# Historico e eventos em disco (SQLite, ja vem no Python - nao precisa instalar nada)
# ==============================================================================
def abrir_banco():
    return contextlib.closing(sqlite3.connect(ARQUIVO_HISTORICO, timeout=5))


def inicializar_banco():
    with abrir_banco() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS leituras ("
            "ts REAL NOT NULL, sensor INTEGER NOT NULL, valor1 REAL, valor2 REAL, ok INTEGER)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_leituras_ts ON leituras(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_leituras_sensor_ts ON leituras(sensor, ts)")
        con.execute(
            "CREATE TABLE IF NOT EXISTS eventos ("
            "ts REAL NOT NULL, tipo TEXT NOT NULL, mensagem TEXT NOT NULL)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos(ts)")
        con.execute(
            "CREATE TABLE IF NOT EXISTS saidas ("
            "ts REAL NOT NULL, resistencia_e1 INTEGER, resistencia_e2 INTEGER, ventoinha INTEGER)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_saidas_ts ON saidas(ts)")
        con.commit()


def salvar_leituras(ts, sensores_lidos, r1, r2, ventoinha):
    with abrir_banco() as con:
        con.executemany(
            "INSERT INTO leituras (ts, sensor, valor1, valor2, ok) VALUES (?, ?, ?, ?, ?)",
            [
                (ts, idx, s["valor1"], s["valor2"], 1 if s["ok"] else 0)
                for idx, s in enumerate(sensores_lidos)
            ],
        )
        con.execute(
            "INSERT INTO saidas (ts, resistencia_e1, resistencia_e2, ventoinha) VALUES (?, ?, ?, ?)",
            (ts, int(r1), int(r2), int(ventoinha)),
        )
        con.commit()


def limpar_dados_antigos():
    limite = time.time() - RETENCAO_DIAS * 86400
    with abrir_banco() as con:
        con.execute("DELETE FROM leituras WHERE ts < ?", (limite,))
        con.execute("DELETE FROM eventos WHERE ts < ?", (limite,))
        con.execute("DELETE FROM saidas WHERE ts < ?", (limite,))
        con.commit()


def restaurar_historico():
    """Reabastece os mini-graficos e a lista de eventos com o que ficou gravado em disco."""
    with abrir_banco() as con:
        for idx in range(len(SENSORES)):
            linhas = con.execute(
                "SELECT valor1, valor2 FROM leituras WHERE sensor = ? AND ok = 1 "
                "ORDER BY ts DESC LIMIT ?",
                (idx, HISTORICO_TAMANHO),
            ).fetchall()
            for v1, v2 in reversed(linhas):
                historico_valores[idx]["valor1"].append(v1)
                historico_valores[idx]["valor2"].append(v2)

        linhas = con.execute(
            "SELECT ts, tipo, mensagem FROM eventos ORDER BY ts DESC LIMIT ?", (EVENTOS_EM_MEMORIA,)
        ).fetchall()
        for ts, tipo, mensagem in reversed(linhas):
            eventos_recentes.append({"ts": ts, "tipo": tipo, "msg": mensagem})


def registrar_evento(tipo, mensagem):
    """tipo: 'info', 'aviso' ou 'alarme' (define a cor no painel)."""
    ts = time.time()
    eventos_recentes.append({"ts": ts, "tipo": tipo, "msg": mensagem})
    print(f"[evento] {mensagem}")
    # Atualiza o painel na hora (mesmo sem leitura nova, ex: conexao caiu). Nunca chamar esta
    # funcao com a "trava" ja segurada, senao trava (Lock nao e reentrante).
    with trava:
        estado["eventos"] = list(reversed(list(eventos_recentes)))[:EVENTOS_NO_PAINEL]
    try:
        with abrir_banco() as con:
            con.execute("INSERT INTO eventos (ts, tipo, mensagem) VALUES (?, ?, ?)", (ts, tipo, mensagem))
            con.commit()
    except Exception as erro:
        print(f"Erro ao gravar evento: {erro}")


_anterior = None  # o que foi lido na rodada anterior, pra perceber mudancas e gerar eventos


def descrever_estagio(sensores_lidos, i):
    topo, base = sensores_lidos[2 * i], sensores_lidos[2 * i + 1]
    return f"topo {topo['valor1']:.1f} °C, base {base['valor2']:.0f}%"


def detectar_eventos(conectado, sensores_lidos, ventoinha, r1, r2, alarme_falha, alarme_sat, ciclos):
    """Compara com a rodada anterior e registra o que mudou (nao guarda o estado antigo em lugar
    nenhum alem da memoria: se o programa reiniciar, a primeira rodada so serve de referencia)."""
    global _anterior
    atual = {
        "conectado": conectado, "ventoinha": ventoinha, "r": [r1, r2], "falha": alarme_falha,
        "sat": alarme_sat, "ciclos": ciclos, "ok": [s["ok"] for s in sensores_lidos],
    }
    ant = _anterior
    if ant is None:
        _anterior = atual
        return

    if not conectado:
        # sem resposta, os valores vem zerados: so avisa da conexao, o resto espera voltar
        if ant["conectado"]:
            registrar_evento("aviso", "Conexão Modbus perdida (ESP32 sem resposta)")
            ant["conectado"] = False
        return
    if not ant["conectado"]:
        registrar_evento("info", "Conexão Modbus estabelecida")

    if ventoinha != ant["ventoinha"]:
        registrar_evento("info", "Ventoinha ligada" if ventoinha else "Ventoinha desligada")

    for i, ligada in enumerate((r1, r2)):
        if ligada != ant["r"][i]:
            acao = "ligada" if ligada else "desligada"
            registrar_evento("info", f"Estágio {i + 1}: resistência {acao} ({descrever_estagio(sensores_lidos, i)})")

    if alarme_falha != ant["falha"]:
        if alarme_falha:
            registrar_evento("alarme", "ALARME: falha de funcionamento")
        else:
            registrar_evento("info", "Alarme de falha de funcionamento normalizado")
    if alarme_sat != ant["sat"]:
        if alarme_sat:
            registrar_evento("alarme", "ALARME: sílica-gel saturada / fim de vida")
        else:
            registrar_evento("info", "Alarme de saturação da sílica normalizado")

    if ciclos > ant["ciclos"]:
        registrar_evento("info", f"Ciclo de regeneração concluído (total: {ciclos})")

    for idx, ok in enumerate(atual["ok"]):
        if ok != ant["ok"][idx]:
            nome = SENSORES[idx]["nome"]
            if ok:
                registrar_evento("info", f"Sensor {nome} voltou a responder")
            else:
                registrar_evento("aviso", f"Sensor {nome} sem leitura")

    _anterior = atual


_controle_anterior = None  # ultimo modo/limites vistos, pra so registrar evento quando mudar de verdade


def publicar_controle(ventoinha2, modo_manual, limites, ciclo_incompleto, desde_seg):
    """Estado do controle do ESP (segunda ventoinha, modo, limites, ciclos pendentes). Registra
    evento quando o modo ou os limites mudam - util pra saber depois quem trocou o que e quando."""
    global _controle_anterior
    ant = _controle_anterior
    if ant is not None:
        if modo_manual != ant["modo"]:
            registrar_evento("info", "Modo MANUAL ativado" if modo_manual else "Modo AUTOMÁTICO ativado")
        limites_mudaram = any(
            abs(limites.get(chave, 0) - ant["limites"].get(chave, 0)) > 0.05
            for chave in ("ligar", "desligar", "temp")
        )
        if limites_mudaram:
            registrar_evento(
                "info",
                f"Limites atualizados: ligar {limites['ligar']:.1f}%, "
                f"desligar {limites['desligar']:.1f}%, temp. máxima {limites['temp']:.1f} °C",
            )
    _controle_anterior = {"modo": modo_manual, "limites": dict(limites)}

    with trava:
        estado["ventoinha2"] = ventoinha2
        estado["modoManual"] = modo_manual
        estado["limites"] = limites
        estado["cicloIncompleto"] = ciclo_incompleto
        estado["desdeSeg"] = desde_seg


_ultima_gravacao = 0.0


def publicar_estado(conectado, sensores_lidos, ventoinha, r1, r2, alarme_falha, alarme_sat, ciclos):
    """Ponto unico que atualiza o estado do painel, o historico em memoria, o historico em disco e
    os eventos (usado tanto pela leitura Modbus real quanto pela simulacao)."""
    global _ultima_gravacao
    agora = time.time()

    for idx, s in enumerate(sensores_lidos):
        if s["ok"]:
            historico_valores[idx]["valor1"].append(s["valor1"])
            historico_valores[idx]["valor2"].append(s["valor2"])

    # O disco recebe uma leitura a cada INTERVALO_GRAVACAO_SEG (nao a cada rodada): gravar tudo
    # a cada 2 s daria centenas de MB em 30 dias.
    if conectado and agora - _ultima_gravacao >= INTERVALO_GRAVACAO_SEG:
        _ultima_gravacao = agora
        try:
            salvar_leituras(agora, sensores_lidos, r1, r2, ventoinha)
        except Exception as erro:
            # Falha ao gravar (disco cheio, arquivo travado) nao pode derrubar a leitura ao vivo.
            print(f"Erro ao gravar historico: {erro}")

    try:
        detectar_eventos(conectado, sensores_lidos, ventoinha, r1, r2, alarme_falha, alarme_sat, ciclos)
    except Exception as erro:
        print(f"Erro ao registrar eventos: {erro}")

    with trava:
        estado["conectado"] = conectado
        estado["sensores"] = sensores_lidos
        estado["ventoinha"] = ventoinha
        estado["resistenciaE1"] = r1
        estado["resistenciaE2"] = r2
        estado["alarmeFalha"] = alarme_falha
        estado["alarmeSaturacao"] = alarme_sat
        # alarme que normalizou perde o reconhecimento: se voltar, pisca de novo
        if not alarme_falha:
            estado["alarmesReconhecidos"]["falha"] = False
        if not alarme_sat:
            estado["alarmesReconhecidos"]["saturacao"] = False
        estado["ciclosSilica"] = ciclos
        estado["ultimaAtualizacao"] = agora
        estado["historico"] = [
            {"valor1": list(h["valor1"]), "valor2": list(h["valor2"])} for h in historico_valores
        ]
        estado["eventos"] = list(reversed(list(eventos_recentes)))[:EVENTOS_NO_PAINEL]


# ==============================================================================
# Leitura Modbus real
# ==============================================================================
def ler_registrador(endereco):
    with trava_modbus:
        r = cliente.read_input_registers(address=endereco, count=1, device_id=ID_ESCRAVO)
    if r.isError():
        return None
    return r.registers[0]


def ler_status(endereco):
    with trava_modbus:
        r = cliente.read_discrete_inputs(address=endereco, count=1, device_id=ID_ESCRAVO)
    if r.isError():
        return False
    return bool(r.bits[0])


def ler_holding(endereco):
    with trava_modbus:
        r = cliente.read_holding_registers(address=endereco, count=1, device_id=ID_ESCRAVO)
    if r.isError():
        return None
    return r.registers[0]


def ler_coil(endereco):
    with trava_modbus:
        r = cliente.read_coils(address=endereco, count=1, device_id=ID_ESCRAVO)
    if r.isError():
        return False
    return bool(r.bits[0])


def loop_leitura():
    ciclos_desde_limpeza = 0
    rodadas_sem_resposta = 0
    while True:
        try:
            with trava_modbus:
                if not cliente.connected:
                    cliente.connect()

            respondeu = False
            sensores_lidos = []
            for s in SENSORES:
                ok = ler_status(s["reg_status"])
                v1 = ler_registrador(s["reg_valor1"])
                v2 = ler_registrador(s["reg_valor2"])
                if v1 is not None or v2 is not None:
                    respondeu = True
                sensores_lidos.append(
                    {
                        "nome": s["nome"],
                        "tipo": s["tipo"],
                        "ok": ok,
                        "valor1": (v1 / 10.0) if v1 is not None else 0,
                        "valor2": (v2 / 10.0) if v2 is not None else 0,
                    }
                )

            ventoinha = ler_coil(REG_COIL_VENTOINHA)
            resistencia_e1 = ler_coil(REG_COIL_RESISTENCIA_E1)
            resistencia_e2 = ler_coil(REG_COIL_RESISTENCIA_E2)

            alarme_falha = ler_status(REG_ALARME_FALHA)
            alarme_saturacao = ler_status(REG_ALARME_SATURACAO)
            ciclos = ler_holding(REG_CICLOS_SILICA)

            if respondeu:
                limites = {}
                for chave, reg in (("ligar", REG_LIMITE_LIGAR), ("desligar", REG_LIMITE_DESLIGAR),
                                   ("temp", REG_TEMP_MAXIMA_MANUAL)):
                    valor = ler_holding(reg)
                    limites[chave] = valor / 10.0 if valor is not None else 0
                publicar_controle(
                    ler_coil(REG_COIL_VENTOINHA2), ler_coil(REG_COIL_MODO_MANUAL), limites,
                    [ler_status(REG_CICLO_INCOMPLETO_E1), ler_status(REG_CICLO_INCOMPLETO_E1 + 1)],
                    [ler_registrador(REG_DESDE_MUDANCA_E1), ler_registrador(REG_DESDE_MUDANCA_E1 + 1)],
                )

            # "Conectado" so vale se o ESP32 realmente respondeu (a porta pode estar aberta e o
            # adaptador desplugado ou o ESP desligado).
            publicar_estado(
                cliente.connected and respondeu, sensores_lidos, ventoinha, resistencia_e1,
                resistencia_e2, alarme_falha, alarme_saturacao, ciclos if ciclos is not None else 0,
            )

            if respondeu:
                rodadas_sem_resposta = 0
            else:
                rodadas_sem_resposta += 1
                if rodadas_sem_resposta >= RODADAS_SEM_RESPOSTA_PRA_RECONECTAR:
                    rodadas_sem_resposta = 0
                    print("Sem resposta do ESP32: reabrindo a porta serial")
                    with trava_modbus:
                        cliente.close()
        except Exception as erro:
            # Nao deixa uma falha pontual (porta ocupada, timeout, etc.) matar a thread pra sempre -
            # fecha a porta (a proxima volta tenta abrir de novo) e marca "desconectado".
            print(f"Erro na leitura Modbus: {erro}")
            try:
                with trava_modbus:
                    cliente.close()
            except Exception:
                pass
            with trava:
                estado["conectado"] = False
            if _anterior is not None and _anterior["conectado"]:
                registrar_evento("aviso", "Conexão Modbus perdida (porta serial indisponível)")
                _anterior["conectado"] = False

        ciclos_desde_limpeza += 1
        if ciclos_desde_limpeza >= CICLOS_ENTRE_LIMPEZAS:
            ciclos_desde_limpeza = 0
            try:
                limpar_dados_antigos()
            except Exception as erro:
                print(f"Erro ao limpar historico antigo: {erro}")

        time.sleep(INTERVALO_LEITURA_SEG)


# ==============================================================================
# Simulacao (python app.py --simular): imita o ESP32 sem hardware
# ==============================================================================
class Simulador:
    """Modelo bem simples de dois estagios de silica-gel + o mesmo controle automatico do firmware
    WiFi (corte a 85 C, so religa abaixo de 70 C, prioridade pra retomar ciclo incompleto, desliga
    quando a base seca). Comandos do painel viram modo manual por 90 s e depois o automatico volta."""

    TEMP_AMBIENTE = 25.0
    LIMITE_LIGAR_UMIDADE = 55.0
    LIMITE_DESLIGAR_UMIDADE = 8.0
    TEMP_DESLIGAR = TEMP_CORTE
    TEMP_RELIGAR = TEMP_RELIGA
    SEGUNDOS_MANUAL = 90

    def __init__(self):
        self.trava = threading.Lock()
        self.temp_topo = [26.0, 26.0]
        self.temp_base = [26.0, 26.0]
        self.carga_topo = [80.0, 32.0]  # umidade "de fundo" do leito, antes do efeito da temperatura
        self.carga_base = [75.0, 30.0]
        self.pico = [0.0, 0.0]  # onda de umidade empurrada pra base no comeco do aquecimento
        self.resistencia = [False, False]
        self.ventoinha = False
        self.ventoinha2 = False
        self.temp_maxima_manual = 50.0
        self.ciclo_incompleto = [False, False]
        self.mudanca_ts = [time.time(), time.time()]  # quando cada resistencia ligou/desligou
        self._resistencia_anterior = [False, False]
        self.ciclos = 128
        self.auto_ativo = True
        self.auto_retoma_em = 0.0

    def comando_coil(self, registrador, ligar):
        with self.trava:
            self.auto_ativo = False
            self.auto_retoma_em = time.time() + self.SEGUNDOS_MANUAL
            if registrador == REG_COIL_VENTOINHA:
                self.ventoinha = ligar
            elif registrador == REG_COIL_RESISTENCIA_E1:
                self.resistencia[0] = ligar
            elif registrador == REG_COIL_RESISTENCIA_E2:
                self.resistencia[1] = ligar
            elif registrador == REG_COIL_VENTOINHA2:
                self.ventoinha2 = ligar

    def comando_modo(self, manual):
        with self.trava:
            self.auto_ativo = not manual
            self.auto_retoma_em = time.time() + self.SEGUNDOS_MANUAL
            if not manual:
                self.ventoinha2 = False

    def comando_limite(self, registrador, valor):
        with self.trava:
            if registrador == REG_LIMITE_LIGAR:
                self.LIMITE_LIGAR_UMIDADE = valor
            elif registrador == REG_LIMITE_DESLIGAR:
                self.LIMITE_DESLIGAR_UMIDADE = valor
            elif registrador == REG_TEMP_MAXIMA_MANUAL:
                self.temp_maxima_manual = valor

    def controle(self):
        with self.trava:
            return (
                self.ventoinha2, not self.auto_ativo,
                {"ligar": self.LIMITE_LIGAR_UMIDADE, "desligar": self.LIMITE_DESLIGAR_UMIDADE,
                 "temp": self.temp_maxima_manual},
                list(self.ciclo_incompleto),
                [int(time.time() - ts) for ts in self.mudanca_ts],
            )

    @staticmethod
    def _fator_temperatura(temp):
        # umidade relativa cai quando esquenta (mesmo vapor, ar aguenta mais)
        return max(0.15, 1.0 - 0.008 * (temp - 25.0))

    def _fisica(self):
        for i in range(2):
            if self.resistencia[i]:
                self.temp_topo[i] += 0.05 * (110.0 - self.temp_topo[i])
                if self.temp_base[i] < 55.0:
                    self.pico[i] += 2.5
                if self.temp_topo[i] > 45.0:
                    self.carga_topo[i] = max(3.0, self.carga_topo[i] - 2.2)
                if self.temp_base[i] > 50.0:
                    self.carga_base[i] = max(2.0, self.carga_base[i] - 2.5)
            else:
                self.temp_topo[i] += 0.012 * (self.TEMP_AMBIENTE - self.temp_topo[i])
                # sem aquecer, o leito volta a absorver devagar (o transformador respira)
                self.carga_topo[i] = min(90.0, self.carga_topo[i] + 0.25)
                self.carga_base[i] = min(90.0, self.carga_base[i] + 0.12)
            # a base acompanha o topo com atraso
            self.temp_base[i] += 0.06 * (self.temp_topo[i] - self.temp_base[i])
            self.pico[i] *= 0.94

    def _umidades(self):
        umid_topo, umid_base = [], []
        for i in range(2):
            ruido = random.uniform(-0.4, 0.4)
            ut = self.carga_topo[i] * self._fator_temperatura(self.temp_topo[i]) + ruido
            ub = (self.carga_base[i] + self.pico[i]) * self._fator_temperatura(self.temp_base[i]) + ruido
            umid_topo.append(max(1.0, min(99.0, ut)))
            umid_base.append(max(1.0, min(99.0, ub)))
        return umid_topo, umid_base

    def _controlar(self, umid_topo, umid_base):
        # corte de seguranca por temperatura (vale em qualquer modo)
        for i in range(2):
            if self.resistencia[i] and self.temp_topo[i] > self.TEMP_DESLIGAR:
                self.resistencia[i] = False
                self.ciclo_incompleto[i] = True

        if not self.auto_ativo:
            return

        desligado_agora = [False, False]
        for i in range(2):
            if self.resistencia[i] and umid_base[i] < self.LIMITE_DESLIGAR_UMIDADE:
                self.resistencia[i] = False
                desligado_agora[i] = True
                self.ciclo_incompleto[i] = False
                self.ciclos += 1

        if not any(self.resistencia):
            escolhido = None
            # prioridade: retomar ciclo incompleto assim que esfriar
            for i in range(2):
                if self.ciclo_incompleto[i] and not desligado_agora[i] and self.temp_topo[i] <= self.TEMP_RELIGAR:
                    escolhido = i
                    break
            if escolhido is None:
                maior = -1.0
                for i in range(2):
                    if self.ciclo_incompleto[i]:
                        continue
                    quer = umid_topo[i] > self.LIMITE_LIGAR_UMIDADE and self.temp_topo[i] <= self.TEMP_RELIGAR
                    if quer and not desligado_agora[i] and umid_topo[i] > maior:
                        maior = umid_topo[i]
                        escolhido = i
            if escolhido is not None:
                self.resistencia[escolhido] = True

        self.ventoinha = any(self.resistencia) or any(self.ciclo_incompleto)

    def passo(self):
        """Avanca um intervalo de leitura e devolve os mesmos dados que a leitura Modbus real."""
        with self.trava:
            if not self.auto_ativo and time.time() >= self.auto_retoma_em:
                self.auto_ativo = True
            self._fisica()
            umid_topo, umid_base = self._umidades()
            self._controlar(umid_topo, umid_base)
            # pega tanto as decisoes do automatico quanto os comandos do painel (entre dois passos)
            for i in range(2):
                if self.resistencia[i] != self._resistencia_anterior[i]:
                    self._resistencia_anterior[i] = self.resistencia[i]
                    self.mudanca_ts[i] = time.time()

            def sensor(idx, v1, v2):
                return {
                    "nome": SENSORES[idx]["nome"],
                    "tipo": SENSORES[idx]["tipo"],
                    "ok": True,
                    "valor1": round(v1, 1),
                    "valor2": round(v2, 1),
                }

            sensores_lidos = [
                sensor(0, self.temp_topo[0], umid_topo[0]),
                sensor(1, self.temp_base[0], umid_base[0]),
                sensor(2, self.temp_topo[1], umid_topo[1]),
                sensor(3, self.temp_base[1], umid_base[1]),
                sensor(4, 24.5 + random.uniform(-0.2, 0.2), 918.0 + random.uniform(-0.3, 0.3)),
                sensor(5, 25.0 + random.uniform(-0.2, 0.2), 45.0 + random.uniform(-0.5, 0.5)),
            ]
            return (
                sensores_lidos, self.ventoinha, self.resistencia[0], self.resistencia[1],
                self.ciclos >= CICLOS_MAXIMOS_SILICA, self.ciclos,
            )


simulador = Simulador() if SIMULAR else None


def loop_simulacao():
    ciclos_desde_limpeza = 0
    while True:
        try:
            sensores_lidos, ventoinha, r1, r2, alarme_sat, ciclos = simulador.passo()
            publicar_controle(*simulador.controle())
            publicar_estado(True, sensores_lidos, ventoinha, r1, r2, False, alarme_sat, ciclos)
        except Exception as erro:
            print(f"Erro na simulacao: {erro}")

        ciclos_desde_limpeza += 1
        if ciclos_desde_limpeza >= CICLOS_ENTRE_LIMPEZAS:
            ciclos_desde_limpeza = 0
            try:
                limpar_dados_antigos()
            except Exception as erro:
                print(f"Erro ao limpar historico antigo: {erro}")

        time.sleep(INTERVALO_LEITURA_SEG)


def escrever_coil(registrador, ligar):
    if SIMULAR:
        simulador.comando_coil(registrador, ligar)
        return
    with trava_modbus:
        r = cliente.write_coil(registrador, ligar, device_id=ID_ESCRAVO)
    if r.isError():
        raise RuntimeError(f"o ESP32 recusou o comando ({r})")


def executar_comando(funcao, *args):
    """Roda uma escrita no ESP e devolve a resposta HTTP: o painel mostra o motivo se falhar
    (porta serial fechada, ESP sem resposta, comando recusado)."""
    try:
        funcao(*args)
    except Exception as erro:
        print(f"Erro ao enviar comando: {erro}")
        return jsonify({"erro": str(erro) or "sem resposta do ESP32"}), 502
    return jsonify({"ok": True})


# ==============================================================================
# Rotas
# ==============================================================================
@app.route("/")
def raiz():
    return render_template("index.html")


@app.route("/api/dados")
def api_dados():
    with trava:
        return jsonify(estado)


@app.route("/api/ventoinha", methods=["POST"])
def api_ventoinha():
    ligar = bool(request.json.get("ligar", False))
    reg = REG_COIL_VENTOINHA2 if request.json.get("id") == 2 else REG_COIL_VENTOINHA
    return executar_comando(escrever_coil, reg, ligar)


def pin_confere(pin):
    return not PIN_AJUSTES or hmac.compare_digest(str(pin or ""), PIN_AJUSTES)


def pin_recusado():
    """Resposta 401 pras rotas protegidas quando o PIN do cabecalho X-Pin nao bate (ou None)."""
    if pin_confere(request.headers.get("X-Pin")):
        return None
    return jsonify({"erro": "PIN incorreto ou ausente"}), 401


@app.route("/api/pin", methods=["POST"])
def api_pin():
    """So confere o PIN (o painel pergunta antes de abrir os ajustes ou trocar o modo)."""
    if pin_confere((request.json or {}).get("pin")):
        return jsonify({"ok": True})
    time.sleep(0.5)  # atrasa tentativa e erro no teclado
    return jsonify({"erro": "PIN incorreto"}), 401


@app.route("/api/alarmes/reconhecer", methods=["POST"])
def api_reconhecer_alarme():
    alarme = (request.json or {}).get("alarme")
    if alarme not in ALARMES:
        return jsonify({"erro": "alarme inexistente"}), 404
    with trava:
        ativo = estado["alarmeFalha"] if alarme == "falha" else estado["alarmeSaturacao"]
        ja_reconhecido = estado["alarmesReconhecidos"][alarme]
        if ativo:
            estado["alarmesReconhecidos"][alarme] = True
    if not ativo:
        return jsonify({"erro": "esse alarme não está ativo"}), 409
    if not ja_reconhecido:
        registrar_evento("info", f"Alarme reconhecido no painel: {ALARMES[alarme]}")
    return jsonify({"ok": True})


@app.route("/api/modo", methods=["POST"])
def api_modo():
    recusa = pin_recusado()
    if recusa:
        return recusa
    manual = bool(request.json.get("manual", False))
    if SIMULAR:
        return executar_comando(simulador.comando_modo, manual)
    return executar_comando(escrever_coil, REG_COIL_MODO_MANUAL, manual)


@app.route("/api/limites", methods=["POST"])
def api_limites():
    """Grava os limites de controle no ESP (o firmware recusa valores invalidos e devolve os
    antigos, entao o painel confere o que ficou na proxima leitura)."""
    recusa = pin_recusado()
    if recusa:
        return recusa
    try:
        ligar = float(request.json["ligar"])
        desligar = float(request.json["desligar"])
        temp = float(request.json["temp"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"erro": "informe ligar, desligar e temp (números)"}), 400
    if not (0 <= desligar < ligar <= 100 and 0 <= temp <= 125):
        return jsonify({"erro": "umidade entre 0 e 100 (desligar menor que ligar) e temp entre 0 e 125"}), 400

    def gravar():
        if SIMULAR:
            simulador.comando_limite(REG_LIMITE_LIGAR, ligar)
            simulador.comando_limite(REG_LIMITE_DESLIGAR, desligar)
            simulador.comando_limite(REG_TEMP_MAXIMA_MANUAL, temp)
            return
        # Os 3 registradores vao num unico quadro (2, 3 e 4 sao consecutivos): o firmware valida
        # o conjunto, e escrever um por vez poderia ser recusado no meio do caminho.
        with trava_modbus:
            r = cliente.write_registers(
                REG_LIMITE_LIGAR,
                [int(round(ligar * 10)), int(round(desligar * 10)), int(round(temp * 10))],
                device_id=ID_ESCRAVO,
            )
        if r.isError():
            raise RuntimeError(f"o ESP32 recusou os limites ({r})")

    return executar_comando(gravar)


@app.route("/api/resistencia", methods=["POST"])
def api_resistencia():
    estagio = request.json.get("estagio")
    ligar = bool(request.json.get("ligar", False))
    reg = REG_COIL_RESISTENCIA_E1 if estagio == 1 else REG_COIL_RESISTENCIA_E2
    return executar_comando(escrever_coil, reg, ligar)


PONTOS_POR_SERIE = 240  # leituras do periodo agrupadas em ate tantas faixas de tempo (media)


def serie_agrupada(con, sensor, desde, horas):
    largura = horas * 3600 / PONTOS_POR_SERIE
    linhas = con.execute(
        "SELECT CAST((ts - ?) / ? AS INTEGER) AS faixa, AVG(valor1), AVG(valor2), MIN(ts) "
        "FROM leituras WHERE sensor = ? AND ok = 1 AND ts >= ? GROUP BY faixa ORDER BY faixa",
        (desde, largura, sensor, desde),
    ).fetchall()
    return [{"ts": ts, "v1": round(v1, 2), "v2": round(v2, 2)} for _, v1, v2, ts in linhas]


def ler_periodo():
    """?horas=H da requisicao, limitado a retencao. Levanta ValueError se nao for numero."""
    horas = float(request.args.get("horas", 1))
    return max(0.05, min(horas, RETENCAO_DIAS * 24))


@app.route("/api/historico")
def api_historico():
    """Serie de um sensor pro grafico grande: ?sensor=<indice>&horas=<periodo>."""
    try:
        sensor = int(request.args.get("sensor", 0))
        horas = ler_periodo()
    except ValueError:
        return jsonify({"erro": "sensor e horas precisam ser números"}), 400
    if not 0 <= sensor < len(SENSORES):
        return jsonify({"erro": "sensor inexistente"}), 404

    desde = time.time() - horas * 3600
    with abrir_banco() as con:
        pontos = serie_agrupada(con, sensor, desde, horas)

    return jsonify({
        "nome": SENSORES[sensor]["nome"],
        "tipo": SENSORES[sensor]["tipo"],
        "horas": horas,
        "agora": time.time(),
        "intervalo_gravacao_seg": INTERVALO_GRAVACAO_SEG,
        "pontos": pontos,
    })


def periodos_ligada(linhas, coluna, intervalo_max):
    """Transforma as amostras gravadas de uma saida em faixas [inicio, fim] em que ficou ligada.
    Um buraco maior que intervalo_max (painel desligado, sem conexao) fecha a faixa."""
    faixas, inicio, anterior = [], None, None
    for linha in linhas:
        ts, ligada = linha[0], linha[coluna]
        if inicio is not None and ts - anterior > intervalo_max:
            faixas.append([inicio, anterior])
            inicio = None
        if ligada and inicio is None:
            inicio = ts
        elif not ligada and inicio is not None:
            faixas.append([inicio, ts])
            inicio = None
        anterior = ts
    if inicio is not None:
        faixas.append([inicio, anterior])
    return faixas


@app.route("/api/tendencia")
def api_tendencia():
    """Grafico de tendencia do painel: topo e base dos dois estagios (sensores 0-3) no periodo
    ?horas=H, mais as faixas em que cada resistencia ficou ligada."""
    try:
        horas = ler_periodo()
    except ValueError:
        return jsonify({"erro": "horas precisa ser número"}), 400

    desde = time.time() - horas * 3600
    with abrir_banco() as con:
        series = [
            {"sensor": idx, "nome": SENSORES[idx]["nome"], "pontos": serie_agrupada(con, idx, desde, horas)}
            for idx in range(4)
        ]
        saidas = con.execute(
            "SELECT ts, resistencia_e1, resistencia_e2 FROM saidas WHERE ts >= ? ORDER BY ts", (desde,)
        ).fetchall()

    intervalo_max = INTERVALO_GRAVACAO_SEG * 3
    return jsonify({
        "horas": horas,
        "agora": time.time(),
        "intervalo_gravacao_seg": INTERVALO_GRAVACAO_SEG,
        "series": series,
        "aquecimento": [periodos_ligada(saidas, 1, intervalo_max), periodos_ligada(saidas, 2, intervalo_max)],
    })


@app.route("/api/exportar.csv")
def api_exportar_csv():
    try:
        horas = float(request.args.get("horas", 24))
    except ValueError:
        horas = 24.0
    horas = max(0.01, min(horas, RETENCAO_DIAS * 24))
    desde = time.time() - horas * 3600

    with abrir_banco() as con:
        linhas = con.execute(
            "SELECT ts, sensor, valor1, valor2, ok FROM leituras WHERE ts >= ? ORDER BY ts, sensor",
            (desde,),
        ).fetchall()

    saida = io.StringIO()
    escritor = csv.writer(saida)
    escritor.writerow(["data_hora", "sensor", "tipo", "temperatura_c", "umidade_pct_ou_pressao_hpa", "leitura_ok"])
    for ts, sensor, v1, v2, ok in linhas:
        if sensor >= len(SENSORES):
            continue
        escritor.writerow([
            datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            SENSORES[sensor]["nome"], SENSORES[sensor]["tipo"], v1, v2, "sim" if ok else "nao",
        ])

    nome = "secador_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
    resposta = Response("﻿" + saida.getvalue(), mimetype="text/csv")  # BOM: Excel le acentos certo
    resposta.headers["Content-Disposition"] = f'attachment; filename="{nome}"'
    return resposta


def iniciar_servidor():
    try:
        from waitress import serve
    except ImportError:
        print("waitress nao instalado - usando o servidor de desenvolvimento do Flask "
              "(pip install waitress pra usar o servidor de producao)")
        app.run(host="0.0.0.0", port=PORTA_HTTP, debug=False)
        return
    print(f"Painel em http://localhost:{PORTA_HTTP} (servidor de producao: waitress)")
    serve(app, host="0.0.0.0", port=PORTA_HTTP, threads=4)


if __name__ == "__main__":
    inicializar_banco()
    try:
        limpar_dados_antigos()
        restaurar_historico()
    except Exception as erro:
        print(f"Erro ao restaurar historico: {erro}")

    registrar_evento("info", "Painel iniciado" + (" (modo simulação)" if SIMULAR else ""))

    if SIMULAR:
        print("*** MODO SIMULACAO: dados ficticios, sem porta serial ***")
        threading.Thread(target=loop_simulacao, daemon=True).start()
    else:
        threading.Thread(target=loop_leitura, daemon=True).start()
    iniciar_servidor()

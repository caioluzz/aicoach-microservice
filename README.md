# AI RunCoach Garmin Microservice

API FastAPI que autentica na Garmin com as credenciais recebidas, baixa atividades e
extrai laps e telemetria de arquivos FIT em memória. Este é um repositório
independente do backend Java.

## Execução local

Requer Python 3.12 (3.11+ deve funcionar, mas o baseline foi validado em 3.12).

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Confirme em `GET http://127.0.0.1:8000/health`. A documentação OpenAPI fica em
`http://127.0.0.1:8000/docs`.

## Endpoints

| Método | Caminho | Estado |
| --- | --- | --- |
| `GET` | `/health` | Verifica se o processo está ativo sem acessar a Garmin |
| `POST` | `/api/garmin/activities` | Recebe `email`, `password` e `limit` (1–100) |
| `POST` | `/api/garmin/workouts/preview` | Compila `workout.v1` sem acessar a Garmin |
| `POST` | `/api/garmin/workouts/deliver` | Cria/reutiliza o workout e agenda a data |
| `PUT` | `/api/garmin/workouts/{workout_id}` | Atualiza o template e refaz o agendamento |
| `POST` | `/api/garmin/workouts/confirm` | Confirma o item no mês do calendário |
| `POST` | `/api/garmin/workouts/{workout_id}/cancel` | Remove calendário e template |

O retorno contém resumo, indicador nominal de teste VDOT, laps e registros de
telemetria. Arquivos FIT ZIP e FIT nativos são aceitos. Credenciais não são
persistidas por este serviço e mensagens de erro não incluem o texto da exceção.

O compilador suporta aquecimento, corrida, recuperação, desaquecimento, duração
por tempo/distância, alvo de pace e grupos repetidos. O marcador `ARC` derivado da
chave idempotente permite reaproveitar um workout depois de resposta perdida. A
biblioteca externa oferece update, unschedule e delete, usados diretamente sem
automação de navegador.

Defina `GARMIN_ADAPTER_API_KEY` no adaptador e no backend para exigir o header
interno `X-Adapter-Key` em todas as rotas `/api/garmin/*`. A chave e as credenciais
nunca são registradas. Para uso fora da mesma máquina, use também TLS ou rede privada.

## Testes

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

A suíte não acessa a Garmin: simula o cliente e cobre healthcheck, validação do
limite, reconhecimento de FIT/ZIP, compilação dos passos, idempotência, upload,
atualização, confirmação e cancelamento.

## Riscos conhecidos

- a integração usa uma biblioteca não oficial e pode sofrer mudanças do Garmin;
- a autenticação compartilhada é opcional para preservar o modo local; sem
  `GARMIN_ADAPTER_API_KEY`, as rotas internas permanecem abertas;
- cada sincronização faz novo login e download dos arquivos, sem cache/retry/backoff;
- falhas de um FIT são degradadas para listas vazias, sem sinalização por atividade;
- `ended_at`, melhor pace e alguns campos dependem de dados que ainda não são
  derivados do resumo Garmin; permanecem nulos no contrato;
- não existe cálculo numérico de VDOT: a marcação é baseada apenas no nome;
- sincronização eficiente, comparação e análise pós-treino pertencem às etapas seguintes.

# Oracle Always Free로 이전하기

이 문서는 **준비 절차**입니다. 이 파일 생성만으로 Oracle 서버가 생성되거나 PC 감시가 중단되지는 않습니다.

## 1. 계정과 무료 VM

1. [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/)에 가입하고 콘솔에 로그인합니다. 본인 정보·결제 수단 확인은 본인이 진행하세요.
2. Compute → Instances → Create instance에서 `codex-reset-alert`라는 이름으로 Linux VM을 만듭니다.
3. Ubuntu 24.04 LTS 또는 22.04 LTS의 **Always Free 적격 이미지**를 선택합니다.
4. Shape는 계정 콘솔에서 무료 한도를 확인한 `VM.Standard.A1.Flex` 1 OCPU / 6GB RAM을 우선 검토합니다. AMD Micro도 후보지만 메모리가 1GB라 Codex CLI 설치·실행 여유가 적습니다. A1은 ARM이므로 CPU 아키텍처에 맞는 Ubuntu 이미지를 선택합니다.
5. 부트 볼륨은 무료 한도 안의 기본 약 50GB로 두고, 유료 추가 볼륨·유료 이미지·부가 서비스를 선택하지 않습니다. 최종 생성 화면의 예상 비용과 무료 적격 표시를 확인하세요. 다른 인스턴스 사용량도 계정 합계에 포함됩니다.
6. 인터넷 통신이 가능한 VCN·public subnet·공인 IP를 구성합니다. 이 봇은 외부 웹으로 요청하므로 외부로 나가는 HTTPS 통신이 필요합니다. 관리용 SSH(22번) 인바운드는 본인 공인 IP로 제한합니다. 웹 서버용 80/443 인바운드는 필요하지 않습니다.
7. SSH 키는 직접 생성하거나 콘솔에서 생성해 **개인 키를 안전한 로컬 경로에 저장**합니다. 개인 키·비밀번호·일회용 인증 코드를 채팅에 붙여넣지 마세요.
8. 서버가 Running 상태가 되면 공인 IP와 Ubuntu 사용자 이름을 확인합니다 (일반적으로 `ubuntu`). 무료 리소스가 부족하면 생성이 실패할 수 있습니다. 유료 Shape로 임의 전환하지 마세요.

무료 조건은 변할 수 있으므로 [Oracle 공식 무료 리소스 문서](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)를 확인하세요. Always Free 서버는 유휴 자원 회수 대상이 될 수 있으며 항상 유지된다는 보장은 없습니다.

## 2. 소스 전송

아래의 `SERVER_IP`, `KEY_PATH`는 실제 값으로 바꿉니다. SSH 첫 연결 시 표시되는 호스트 키 지문을 확인하고 연결합니다. host-key 검증을 비활성화하지 마세요.

PC 프로젝트 PowerShell:

```powershell
ssh -i KEY_PATH ubuntu@SERVER_IP
# 서버에서 아래 실행 후 exit
mkdir -p ~/codex_reset_alert_bot
exit
# 다시 PC에서 소스 전용 압축 생성·전송
.\deploy\pack-server.ps1
scp -i KEY_PATH data/deploy/bot-source.tar.gz ubuntu@SERVER_IP:~/
ssh -i KEY_PATH ubuntu@SERVER_IP
tar -xzf ~/bot-source.tar.gz -C ~/codex_reset_alert_bot
cd ~/codex_reset_alert_bot
bash deploy/install-linux.sh
```

압축에는 `.env`, `.venv`, SQLite DB, Codex 로그인 토큰을 넣지 않습니다. Windows 가상환경은 Linux로 복사하지 않습니다.

## 3. 서버 인증과 연결 검증

서버의 동일 Ubuntu 사용자로:

```bash
export PATH="$HOME/.local/bin:$PATH"
codex login --device-auth
codex login status
```

출력되는 공식 인증 주소를 본인 브라우저에서 열고 코드를 입력해 로그인합니다. 필요하면 ChatGPT 보안 설정에서 device-code 로그인을 활성화하세요. [공식 인증 문서](https://learn.chatgpt.com/docs/auth)

PC의 `.env`는 토큰을 포함하므로 SCP로 필요한 서버에만 별도 전송합니다. 서버에서 받은 파일은 본인 계정만 읽게 합니다.

```powershell
# PC에서 실행. 원격 .env가 이미 있으면 내용을 확인한 후 전송하세요.
scp -i KEY_PATH .env ubuntu@SERVER_IP:~/codex_reset_alert_bot/.env
```

```bash
# 서버에서 실행
cd ~/codex_reset_alert_bot
chmod 600 .env
nano .env
# CODEX_COMMAND가 Windows 경로이면 공란으로 수정. CODEX_MODEL은 기존대로 가능.
.venv/bin/python -B bot.py --test-telegram
.venv/bin/python -B bot.py --preview-ai
```

둘 다 성공해야 다음 단계로 갑니다. 서버 IP에서는 X 접속이 제한될 수 있으므로 로컬 성공만으로 서버 성공을 가정하지 않습니다. 이 단계는 미리보기/연결 시험이며 실제 새 글 감시를 시작하지 않습니다.

## 4. 처리 기록 이전과 감시 전환

서버 연결 시험에 성공한 다음에만 PC 예약 작업을 중지합니다. PC와 서버에서 동시에 실제 감시하지 마세요. 미리보기는 가능합니다.

PC 프로젝트 PowerShell:

```powershell
Stop-ScheduledTask -TaskName CodexResetAlertBot
# 재로그인 시 PC 봇이 다시 켜지지 않게 비활성화
Disable-ScheduledTask -TaskName CodexResetAlertBot
```

예약 작업이 중지되었는지 확인하고 `data/state.sqlite3`를 서버 `~/codex_reset_alert_bot/data/state.sqlite3`로 전송합니다. 다른 `STATE_PATH`를 쓴다면 그 DB를 전송하세요. SQLite에 접근하는 다른 수동 봇이 있다면 그것도 종료해야 합니다. 서버 서비스가 이미 실행 중이면 먼저 중지해야 DB를 교체할 수 있습니다.

```powershell
scp -i KEY_PATH data/state.sqlite3 ubuntu@SERVER_IP:~/codex_reset_alert_bot/data/state.sqlite3
```

서버에서:

```bash
cd ~/codex_reset_alert_bot
chmod 600 data/state.sqlite3
bash deploy/install-service.sh
journalctl --user -u codex-reset-alert.service -f
```

`systemctl`의 active 상태만으로 성공을 판단하지 마세요. X 조회·Codex 연결 시험·Telegram의 **재시작 성공 메시지**까지 확인해야 이전 완료입니다. 새 글이 없을 때도 시작 시 연결 시험을 한 번 수행합니다. 로그에서 오류가 없으면 SSH 접속을 종료해도 감시는 계속 실행됩니다. 서비스는 서버 재부팅 후에도 시작합니다.

서버 실행이 실패하면 서버 서비스를 중지한 후 PC를 다시 활성화할 수 있습니다:

```bash
systemctl --user disable --now codex-reset-alert.service
```

```powershell
Enable-ScheduledTask -TaskName CodexResetAlertBot
Start-ScheduledTask -TaskName CodexResetAlertBot
```

서버에서 이미 새 글을 처리했다면 최신 DB를 PC로 가져온 후 복귀해야 중복 알림을 줄일 수 있습니다.

## 관리

```bash
systemctl --user status codex-reset-alert.service
systemctl --user restart codex-reset-alert.service
systemctl --user stop codex-reset-alert.service
journalctl --user -u codex-reset-alert.service --since today
```

봇 파일 로그는 `data/bot.log`입니다. 코드·DB를 백업하되 `.env`와 Codex 인증 파일은 비밀로 관리하세요. 무료 서버여도 Codex 계정 이용 한도는 그대로 적용됩니다.

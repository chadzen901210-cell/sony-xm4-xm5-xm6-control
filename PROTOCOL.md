# Sony WH-1000XM4 / XM5 / XM6 控制協定筆記

本文件整理本專案（`battery` / `nc` / `ambient` / `status` / `eq` 與 App 視窗）所需的協定資訊。
§1–§10 以實機測試過的 WH-1000XM5 為主；WH-1000XM4 與 WH-1000XM6 的差異在 §11。
每一項都標出出處；**⚠️ 標記的是尚未確定、需要實機驗證的地方**，程式不應把它們當成已知事實。

## 0. 參考來源與版本

| 代號 | 專案 | 版本 (commit) | 授權 |
|---|---|---|---|
| **GB** | [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge)（Codeberg） | `f2c3d486`（master, 2026-10-24） | AGPL-3.0 |
| **MOS** | [mos9527/SonyHeadphonesClient](https://github.com/mos9527/SonyHeadphonesClient)（branch `v1-compat`） | `8f4808cb`（2026-10-20） | 見該 repo |
| **PB** | [Plutoberth/SonyHeadphonesClient](https://github.com/Plutoberth/SonyHeadphonesClient)（已封存） | `5620e8ed` | 見該 repo |

GB 的路徑以下省略共同前綴 `app/src/main/java/nodomain/freeyourgadget/gadgetbridge/`。
PB 只支援舊的 V1 協定（XM3/XM4），本文件只用它來交叉驗證封包框架，不用它的指令格式。

> 本專案只參考上述專案得出的**協定事實**，程式碼為自行撰寫，不複製其原始碼。

---

## 1. 傳輸層

| 項目 | 內容 | 出處 |
|---|---|---|
| 傳輸 | 傳統藍牙 **RFCOMM**（SPP 風格的位元組串流），不是 BLE | GB `service/devices/sony/headphones/SonyHeadphonesSupport.java`（`SonyHeadphonesSupport` 繼承 `AbstractHeadphoneSerialDeviceSupportV2`） |
| Service UUID V2 | `956C7B26-D49A-4BA8-B03F-B17D393CB6E2` | GB `SonyHeadphonesSupport.BTRFCOMM_UUID_V2`；MOS `libmdr/include/mdr-c/Base.h` `MDR_SERVICE_UUID_V2`（註解：「MDR V2, XM5s and newer」） |
| Service UUID V1 | `96CC203E-5068-46AD-B32D-E316F5E069BA` | GB `SonyHeadphonesSupport.BTRFCOMM_UUID_V1`；MOS `Base.h` `MDR_SERVICE_UUID_V1`（「XM4s and older」）；PB `Client/Constants.h` `SERVICE_UUID` |
| 選擇規則 | GB：SDP 列出 V2 就用 V2，否則 V1；XM5 的 coordinator 沒有覆寫 `preferServiceV2()`（預設 false），所以完全依 SDP 結果。MOS 自動模式：先試 V2 再試 V1 | GB `SonyHeadphonesSupport.getSupportedService()`、`devices/sony/headphones/SonyHeadphonesCoordinator.kt` `preferServiceV2()`、`devices/sony/headphones/coordinators/SonyWH1000XM5Coordinator.java`；MOS `client/Client.cpp`（`services = {V2, V1}`） |
| BLE UUID | `5B833E20-…`（XM5 也有 BLE GATT 介面，**本專案不用**） | MOS `Base.h` `MDR_SERVICE_UUID_BLE` |

**已在本機確認（2026-10-24）**：Windows 為已配對的 XM5 記錄的服務中，有 V2 UUID `956C7B26-…`（Windows 顯示名稱「Serial HPC」），**沒有** V1 UUID。
查詢方式：PowerShell `Get-PnpDevice | Where-Object InstanceId -match '<MAC 去掉冒號>'`，每個服務會以 `BTHENUM\{<UUID>}…` 出現。

**本專案做法**：依藍牙名稱認出型號（§11），先連該型號的服務（XM4：V1；XM5、XM6：V2）；Windows 回報找不到服務（`WSASERVICE_NOT_FOUND`，10108）時，再試另一個。實際使用哪一版協定，最後由初始化回覆的長度決定（§5）。

### Windows 上的連線方式（實作備註）

- Plutoberth 與 mos9527 的 Windows 版都用 `SOCKADDR_BTH`，把 **service UUID 填進 `serviceClassId`、`port = 0`**，讓 Windows 自己做 SDP 查詢後 `connect()`。
  出處：PB `Client/windows/WindowsBluetoothConnector.cpp`；MOS `libmdr-bt/src/Windows/ConnectionWindows.cpp` `MDRConnectionWindows::Connect`（另外設定了 `SO_BTH_AUTHENTICATE` / `SO_BTH_ENCRYPT`）。
- ⚠️ Python 標準函式庫的 `socket.connect((mac, port))` 在 Windows 只能填 RFCOMM **channel 號碼**，無法帶 UUID。
  預計做法：用 Python 建立 `AF_BLUETOOTH` socket，再透過 `ctypes` 呼叫 `ws2_32.connect`，傳入帶有 UUID 的 `SOCKADDR_BTH`（與上面兩個 C++ 實作同一招）。尚未在 Python 驗證，屬於第 3 步要確認的事。
- GB 在連線後等 500 ms 才送資料（註解：「Connecting too fast fails with an IOException」）。出處：GB `SonyHeadphonesSupport.getConnectDelayMillis()`。這是 Android 上的經驗值，Windows 是否需要 ⚠️ 未知。

---

## 2. 封包框架

```
3E | type | seq | len (4 bytes, big-endian) | payload (len bytes) | checksum | 3C
     \_____________________ 這一段要跳脫 _______________________________/
```

| 欄位 | 大小 | 說明 |
|---|---|---|
| 開頭 | 1 | `0x3E`（不跳脫） |
| type | 1 | 訊息型別，見 §3 |
| seq | 1 | 序號，只會是 0 或 1，見 §4 |
| len | **4** | payload 長度，**32-bit big-endian**（不是 1 byte） |
| payload | len | 第一個 byte 是指令碼（command） |
| checksum | 1 | `type + seq + len(4 bytes) + payload` 的總和取低 8 位；**不含開頭 0x3E**；以「跳脫前」的值計算 |
| 結尾 | 1 | `0x3C`（不跳脫） |

**跳脫規則**（套用在 type 到 checksum 之間的每個 byte，checksum 本身也要跳脫）：

| 原始 byte | 送出 |
|---|---|
| `0x3C` | `3D 2C` |
| `0x3D` | `3D 2D` |
| `0x3E` | `3D 2E` |

等同於「送出 `0x3D`，接著送 `原值 & 0xEF`」；解跳脫為 `0x3D` 後一個 byte `| 0x10`。
GB 解跳脫時對任何 byte 都 `| 0x10`；MOS 與 PB 只接受 `2C/2D/2E`，其他值視為錯誤。**本專案採嚴格版**（遇到其他值就丟棄該封包）。

**接收端解析**：在串流中找 `0x3E`，往後找第一個 `0x3C` 即為一個完整封包（因為內容中的 `3C` 已被跳脫，不會誤判），解跳脫後驗 checksum 與長度。

出處：GB `service/devices/sony/headphones/protocol/Message.java`（`Message.encode()`、`encodeMessage()`、`escape()`、`unescape()`、`calcChecksum()`、`fromBytes()`），串流切割見 GB `SonyHeadphonesSupport.onSocketRead()`；MOS `libmdr/src/Command.cpp`（`MDRPackCommand`、`MDRUnpackCommand`、`Escape`、`Unescape`、`Checksum`）、`libmdr/include/mdr/Command.hpp`（`kEscapedByteSentry = 0x3D`、`kEscaped60/61/62 = 44/45/46`）；PB `Client/CommandSerializer.cpp`（`packageDataForBt`、`_escapeSpecials`）。三者一致。

### 範例（可直接當單元測試向量）

| 說明 | bytes | 來源 |
|---|---|---|
| GET audio codec，seq=1 | `3e 0c 01 00 00 00 02 12 02 23 3c` | GB 測試 `SonyProtocolImplV2Test.getAudioCodec()`（`Message.fromBytes` 會驗 checksum，所以此向量必為有效） |
| GET quick access，seq=0 | `3e 0c 00 00 00 00 02 f6 0d 11 3c` | GB 測試 `SonyProtocolImplV2Test.getQuickAccess()`（checksum 進位：0x111 → 0x11） |
| payload 含 `0x3D` | `3e 0c 00 00 00 00 01 3d 2d 4a 3c` | 依規則推導（payload `[3D]`，checksum 0x4A） |
| checksum 本身需跳脫 | `3e 0c 00 00 00 00 01 30 3d 2d 3c` | 依規則推導（payload `[30]`，checksum = 0x3D） |

---

## 3. 訊息型別（type byte）

| 值 | GB 名稱 | MOS 名稱 | 用途 |
|---|---|---|---|
| `0x01` | `ACK` | `ACK` | 確認收到，payload 長度 0 |
| `0x0C` | `COMMAND_1` | `DATA_MDR`（Table 1） | 本專案用到的指令全部走這個 |
| `0x0E` | `COMMAND_2` | `DATA_MDR_NO2`（Table 2） | 語音提示、配對管理等；**本專案不送** |

出處：GB `protocol/MessageType.java`（`MessageType`）；MOS `libmdr/include/mdr/Protocol.hpp`（`MDRDataType`）；PB `Client/Constants.h`（`DATA_TYPE`）。

---

## 4. 序號與 ACK 規則

1. 主機（電腦）第一個封包用 `seq = 0`。
2. 耳機收到主機的 `0x0C` 封包後，回一個 `ACK`，其 `seq = 1 - 原封包 seq`。
3. 主機**下一個要送的封包就用這個 ACK 的 seq**。所以 seq 在 0/1 之間交替。
4. 收到 ACK 的 seq 與「目前 seq」相同 → 視為過期/重複的 ACK，忽略。
5. 耳機主動送來的 `0x0C` / `0x0E` 封包（回覆或通知），主機必須回 `ACK`，`seq = 1 - 該封包 seq`。
6. 一次只能有一個未確認的指令：收到 ACK 之後才送下一個。

出處：GB `service/devices/sony/headphones/SonyHeadphonesProtocol.java`（`decodeResponse()` 的 ACK 分支、`encodeAck()`、`handleAck()`、`pendingAcks` / `requestQueue`）；MOS `libmdr/src/Headphones.cpp`（`MDRHeadphones::Handle`、`SendACK`：`1 - seq`、`HandleAck`：只接受 `1 - mSeqNumber`）。兩者一致。

ACK 範例（依規則推導）：

| 情況 | bytes |
|---|---|
| 回覆 seq=0 的封包（ACK seq=1） | `3e 01 01 00 00 00 00 02 3c` |
| 回覆 seq=1 的封包（ACK seq=0） | `3e 01 00 00 00 00 00 01 3c` |

⚠️ 注意「回覆資料」與「ACK」是兩個不同的封包：例如送出 GET battery 後，會先收到 ACK，再收到一個 `0x0C` 的回覆封包（需要再回 ACK）。兩者到達順序 ⚠️ 未在文件中明確保證，程式要能處理任一順序。

---

## 5. 初始化交握

連線後先送「protocol info」請求，等耳機回覆再送其他指令。

| 方向 | payload | 說明 |
|---|---|---|
| 主機 → 耳機 | `00 00` | GB `SonyHeadphonesProtocol.encodeInit()`；MOS `ConnectGetProtocolInfo`（`CONNECT_GET_PROTOCOL_INFO = 0x00`，`type = FIXED_VALUE 0x00`） |
| 耳機 → 主機 | `01 00 xx xx xx xx xx xx`（8 bytes） | GB 依長度判斷版本：4 bytes = V1、**8 bytes = V2**。GB 原始碼註解記錄的實測值：`WH-1000XM5 1.1.3: 01:00:03:00:00:00:00:00` |

完整封包（seq=0，依規則推導）：`3e 0c 00 00 00 00 02 00 00 0e 3c`

出處：GB `SonyHeadphonesProtocol.decodeResponse()`（init reply 與版本判斷）、`PayloadTypeV1.INIT_REQUEST/INIT_REPLY`；MOS `libmdr/include/mdr/ProtocolV2T1.hpp` `ConnectRetProtocolInfo`（`[0x01, type, protocolVersion Int32BE, supportTable1, supportTable2]`）。

- GB 註解說耳機「有時會忽略第一次 init」，所以每 1.25 秒重送，最多再重送 2 次。出處：GB `SonyHeadphonesSupport.initSendRunnable` / `scheduleInitRetry()`。本專案照做。
- ⚠️ 不送 init 能不能直接下指令：沒有來源說明；兩個實作都一定先送，本專案也一定先送。
- ⚠️ 8 bytes 回覆中 `protocolVersion` 的實際意義（MOS 視為 Int32BE）不確定；本專案只用「長度 = 8」確認是 V2，不解讀版本號。
- **本機實測（2026-10-24）**：回覆為 `01 00 03 00 20 16 00 00`，第 5–6 byte 與 GB 記錄的 fw 1.1.3 樣本（`… 00 00 …`）不同；GB 記錄的其他機種在這兩個位置也各不相同，推測與韌體有關（⚠️ 未證實）。順序是先收到 ACK、再收到回覆，兩者分屬不同的 `recv()`。

---

## 6. 指令

以下 payload 都放在 type = `0x0C` 的封包中。XM5 屬於 V2 協定。

### 6.1 電量（`sonyxm battery`）

| 動作 | payload | 出處 |
|---|---|---|
| 查詢 | `22 00` | GB `PayloadTypeV2.BATTERY_LEVEL_REQUEST (0x22)`、`SonyProtocolImplV2.getBattery(BatteryType.SINGLE)`（`encodeBatteryType(SINGLE) = 0x00`；XM5 coordinator 的能力是 `BatterySingle`）；GB 測試 `SonyProtocolImplV2Test.getBattery()` 期望 `22:00`；MOS `Command::POWER_GET_STATUS = 0x22`、`PowerInquiredType::BATTERY = 0x00` |
| 回覆 | `23 00 <電量 0-100> <充電狀態>` | GB `PayloadTypeV2.BATTERY_LEVEL_REPLY (0x23)` → `SonyProtocolImplV1.handleBattery()`（`payload[2]` 電量、`payload[3] == 1` 表示充電中）；MOS `PowerRetStatusBattery` |
| 主動通知 | `25 00 <電量> <充電狀態>` | GB `PayloadTypeV2.BATTERY_LEVEL_NOTIFY (0x25)`（同一個 handler）；MOS `POWER_NTFY_STATUS = 0x25` |

充電狀態：GB 只判斷 `1 = 充電中`。MOS 的 `BatteryChargingStatus`：`0 未充電、1 充電中、2 未知、3 已充飽`。⚠️ XM5 實際會不會送 2 或 3 未確認。

查詢封包（seq=1，依規則推導）：`3e 0c 01 00 00 00 02 22 00 31 3c`

**本機實測（2026-10-24，播放音樂中）**：送出上面這個封包後，先收到 `ACK seq=0`，再收到 `3e 0c 00 00 00 00 04 23 00 64 00 97 3c`，也就是 payload `23 00 64 00` = 電量 100%、未充電，格式與上表一致。

### 6.2 降噪 / 環境音（`sonyxm nc` / `sonyxm ambient` / `sonyxm status`）

指令碼（兩個來源一致）：

| 指令碼 | GB | MOS |
|---|---|---|
| `0x66` | `AMBIENT_SOUND_CONTROL_GET` | `NCASM_GET_PARAM` |
| `0x67` | `AMBIENT_SOUND_CONTROL_RET` | `NCASM_RET_PARAM` |
| `0x68` | `AMBIENT_SOUND_CONTROL_SET` | `NCASM_SET_PARAM` |
| `0x69` | `AMBIENT_SOUND_CONTROL_NOTIFY` | `NCASM_NTFY_PARAM` |

出處：GB `protocol/impl/v1/PayloadTypeV1.java`（V2 實作沿用這幾個碼）；MOS `ProtocolV2T1.hpp` `enum class Command`。

#### ⚠️ 子型別（payload 第 2 個 byte）兩個來源不同

| 來源 | XM5 使用的子型別 | 依據 |
|---|---|---|
| GB | **`0x15`** | `SonyProtocolImplV2.getAmbientSoundControl()` / `setAmbientSoundControl()`：只有具備 `WindNoiseReduction` 或 `AmbientSoundControl2` 能力的裝置才用 `0x17`；`SonyWH1000XM5Coordinator.getDefaultCapabilities()` 兩者都沒有 → `0x15` |
| MOS | **`0x17`**（`MODE_NC_ASM_DUAL_NC_MODE_SWITCH_AND_ASM_SEAMLESS`） | `libmdr/src/HeadphonesV2.cpp`：依耳機回報的支援功能表選型別，一般 NC/ASM 耳機走 `NcAsmSetParamModeNcDualModeSwitchAsmSeamless`（型別預設 `0x17`）；`docs/device-support/WH-1000XM5.md` 回報 XM5 的「NC/AMB Settings ✅」 |

MOS 的列舉中 `0x15` 名為 `MODE_NC_ASM_AUTO_NC_MODE_SWITCH_AND_ASM_SEAMLESS`（`ProtocolV2T1.hpp` `NcAsmInquiredType`）。

**好消息**：兩者在 7 byte 版本下的欄位排列**完全相同**，只差第 2 個 byte。

**本機實測結果（2026-10-24，`sonyxm status --probe --debug`）：XM5 使用 `0x17`。**

| 送出 | 耳機回應 |
|---|---|
| `66 15` | 只回 ACK，2 秒內**沒有** `67` 回覆；約 0.3 秒後主動送來通知 `69 17 00 01 01 00 14`（子型別 `0x17`，byte[2] = `00`） |
| `66 17` | ACK，接著回覆 `67 17 01 01 01 00 14` = 環境音、強度 20、不專注人聲 |

本專案因此先用 `0x17`，`0x15` 只作為其他韌體的備援。GB 對 XM5 用 `0x15` 可能是舊韌體的行為（⚠️ 未證實）。

#### Payload 格式（7 bytes；GET 回覆、SET、NOTIFY 相同排列）

| 索引 | 意義 | 值 | GB 名稱 | MOS 欄位 |
|---|---|---|---|---|
| 0 | 指令碼 | `67` / `68` / `69` | | `command` |
| 1 | 子型別 | `15` 或 `17`（見上） | | `type` |
| 2 | 值變更狀態 | `01` = 已確定；`00` = 拖動滑桿中 | GB 固定送 `0x01`（註解：「0x00 while dragging the slider?」） | `valueChangeStatus`（`UNDER_CHANGING=0`、`CHANGED=1`） |
| 3 | 總開關 | `00` 關、`01` 開 | Mode OFF ↔ `00` | `ncAsmTotalEffect` |
| 4 | 模式 | `00` 降噪、`01` 環境音 | | `ncAsmMode`（`NC=0`、`ASM=1`） |
| 5 | 專注人聲 | `00` 否、`01` 是 | `focusOnVoice` | `ambientSoundMode`（`NORMAL=0`、`VOICE=1`） |
| 6 | 環境音強度 | 數值（見下方 ⚠️） | `ambientSound` | `ambientSoundLevelValue` |

查詢：`66 15`（或 `66 17`）。

狀態判讀：

| byte[3] | byte[4] | 狀態 |
|---|---|---|
| `00` | 任意 | 關閉（降噪與環境音都關） |
| `01` | `00` | 降噪開 |
| `01` | `01` | 環境音，強度 = byte[6] |

出處：GB `SonyProtocolImplV2.setAmbientSoundControl()`（組 payload）、`SonyProtocolImplV2.handleAmbientSoundControl()`（解析：`payload[3]` 開關、`payload[4]` 模式、倒數第 2 byte 專注人聲、最後 1 byte 強度）；MOS `ProtocolV2T1.hpp` `NcAsmRetParamModeNcDualModeSwitchAsmSeamless` / `NcAsmSetParamModeNcDualModeSwitchAsmSeamless`。

CLI 對應：

| 指令 | 送出（XM5 子型別 `17`；L/F = 先查詢得到的目前強度 / 專注人聲） |
|---|---|
| `sonyxm nc on` | `68 17 01 01 00 F L` |
| `sonyxm nc off` | `68 17 01 00 00 F L`（降噪與環境音都關） |
| `sonyxm ambient N` | `68 17 01 01 01 F N` |
| `sonyxm status` | 送 `66 17`（沒回覆再試 `66 15`），解析 `67` 回覆 |

寫入後程式會再查詢一次，確認耳機回報的狀態與要求的一致。

**本機實測寫入（2026-10-24）**：`sonyxm nc on --debug`（原本是環境音強度 20）

| 時間 | 方向 | 內容 |
|---|---|---|
| 1.406 | >> | `68 17 01 01 00 00 14`（降噪，強度欄位保留 20） |
| 1.453 | << | ACK |
| 1.453 | << | 主動通知 `69 17 01 01 00 00 14`（已是降噪） |
| 1.484 | << | 讀回 `67 17 01 01 00 00 14` ✅ |

`ambient 10`、`ambient 20 --voice`、`nc off` 也都成功；每次寫入後耳機都會先回 ACK，再主動送一個 `69` 通知。

切到降噪或關閉時仍要填強度與專注人聲欄位（兩個來源都是如此）；本專案先查詢目前值再原樣帶回，避免把使用者原本的環境音設定洗掉。⚠️ 此「先讀再寫」是本專案的設計，不是來源中的行為。

#### 環境音強度範圍：1–20（已實測）

- GB `devices/sony/headphones/prefs/AmbientSoundControl.java`：建構子接受 `0–20`，`SonyProtocolImplV2.handleAmbientSoundControl()` 也接受 `0–20`；但 GB 的設定 UI 實際範圍是 `1–20`（偏好值 +1，註解說是 SeekBar 最小值限制）。
- MOS 對這個欄位沒有範圍標註。
- **本機實測（2026-10-24）**：寫入 `68 17 01 01 01 01 00`（強度 0）後，耳機回 ACK，但通知 `69 17 01 01 01 01 01` 與讀回 `67 17 01 01 01 01 01` 都顯示**強度 1**。耳機會把 0 自動調成 1。
- 因此本專案寫入時只允許 `1–20`（白名單同步收緊）；解析回覆時仍接受 `0–20`。

---

### 6.3 等化器（`sonyxm eq`）

| 指令碼 | GB | MOS |
|---|---|---|
| `0x56` | `EQUALIZER_GET` | `EQEBB_GET_PARAM` |
| `0x57` | `EQUALIZER_RET` | `EQEBB_RET_PARAM` |
| `0x58` | `EQUALIZER_SET` | `EQEBB_SET_PARAM` |
| `0x59` | `EQUALIZER_NOTIFY` | `EQEBB_NTFY_PARAM` |

第 2 個 byte 一律是 `0x00`（MOS：`EqEbbInquiredType::PRESET_EQ`）。

| 動作 | payload | 出處 |
|---|---|---|
| 查詢 | `56 00` | GB `SonyProtocolImplV2.getEqualizer()`；MOS `EqEbbGetParam` |
| 回覆／通知 | `57`/`59` `00 <preset> <數量> <值…>`；XM5 數量為 6：Clear Bass、400 Hz、1 kHz、2.5 kHz、6.3 kHz、16 kHz，每個值為 −10..+10 再加 10 | GB `SonyProtocolImplV1.handleEqualizer()`；MOS `EqEbbRetParamEq` / `EqParam{presetId, bandSteps[]}`、`HeadphonesV2T1.cpp` `HandleEqEbbParamT1()`（6 個值時第一個是 Clear Bass） |
| 切換預設 | `58 00 <preset> 00`（數量 0，耳機套用預設本身的值） | GB `setEqualizerPreset()`；MOS `HeadphonesV2.cpp` 只設定 `presetId` |
| 自訂頻段 | `58 00 <a0\|a1\|a2> 06 <CB> <400> <1k> <2.5k> <6.3k> <16k>`（各值 +10） | GB `setEqualizerCustomBands()`（固定用 `0xa0`）；MOS 同樣格式，但帶目前的 presetId |

預設代碼（GB `prefs/EqualizerPreset.java`，與 MOS `EqPresetId` 一致）：`00` 關閉、`10` Bright、`11` Excited、`12` Mellow、`13` Relaxed、`14` Vocal、`15` Treble Boost、`16` Bass Boost、`17` Speech、`a0` Manual、`a1` Custom 1、`a2` Custom 2。

`a0`/`a1`/`a2` 的名稱依照 Sony App：`a0` 是「手動」，`a1`/`a2` 是兩組可存的「自訂 1/2」。GB 把 `a0` 叫 Custom、`a1`/`a2` 叫 User 1/2，本專案改用 Sony App 的叫法。只有這三個代碼能寫入頻段值；目前若在 `a1`/`a2` 就寫回同一組，否則寫到 `a0`（與 MOS 帶目前 presetId 的做法一致）。

測試向量：GB `SonyProtocolImplV2Test.setEqualizerPreset()`、`setEqualizerCustomBands()`、`handleEqualizer()` 的完整封包，都已放進本專案的單元測試。

✅ 實機驗證（2026-10-24）：XM5 回覆 6 個值（Clear Bass + 5 頻段）；讀到的目前預設是 `a1`（使用者在 Sony App 裡用的 Custom 1）；在 App 視窗中切換預設與拖動頻段，邊聽可以聽出差別。

---

## 7. 安全：只送白名單內的指令

程式中的編碼層只允許組出以下封包，其他一律拒絕（直接 raise，不送出）。
白名單**依協定版本分開**：同一個指令碼在 V1、V2 的意思可能不同，最危險的例子是 `0x22`——V2 的「查詢電量」，在 V1 是「關機」（GB `PayloadTypeV1.POWER_OFF`）。
所以連線物件在交握前只允許送初始化查詢，交握後依耳機回報的版本套用對應的白名單。

ACK（type `0x01`、payload 空）兩版都允許。以下皆為 type `0x0C`：

| 版本 | payload | 用途 |
|---|---|---|
| 交握前 | `00 00` | 初始化查詢（只允許這個） |
| V2 | `00 00` | 初始化查詢 |
| V2 | `22 <00\|08>` | 查詢電量（`08` 見 §11.3） |
| V2 | `66 <15\|17\|19>` | 查詢 NC/環境音 |
| V2 | `68 <15\|17> 01 <00\|01> <00\|01> <00\|01> <1..20>` | 設定 NC/環境音 |
| V2 | `68 19 01 <00\|01> <00\|01> <00\|01> <1..20> <00\|01> <00..02>` | 設定 NC/環境音（XM6，§11.2） |
| V2 | `56 00` | 查詢等化器 |
| V2 | `58 00 <§6.3／§11.4 的預設代碼> 00` | 切換等化器預設 |
| V2 | `58 00 <a0\|a1\|a2> 06 <6 個 0..20>` | 自訂等化器頻段（Clear Bass + 5 段） |
| V2 | `58 00 <a0\|a1\|a2> 0a <10 個 0..12>` | 自訂等化器頻段（XM6 的 10 段，§11.4） |
| V1 | `00 00` | 初始化查詢 |
| V1 | `10 00` | 查詢電量 |
| V1 | `66 02` | 查詢 NC/環境音 |
| V1 | `68 02 <00\|11> 02 <00..02> 01 <00\|01> <0..20>` | 設定 NC/環境音（§11.2） |
| V1 | `56 01` | 查詢等化器 |
| V1 | `58 01 <預設代碼> 00` | 切換等化器預設 |
| V1 | `58 01 ff 06 <6 個 0..20>` | 自訂等化器頻段 |

**明確禁止**（列出來源中看到的危險指令，作為反例寫進測試）：

| payload | 作用 | 出處 |
|---|---|---|
| `30`–`3F` 開頭 | 韌體更新（`UPDT_*`） | MOS `ProtocolV2T1.hpp` `Command::UPDT_*` |
| `f8 09 01` | 恢復原廠設定 | GB `PayloadTypeV2.FACTORY_RESET_SET (0xf8)`、`SonyProtocolImplV2.factoryReset()` |
| `98 00 16 01` | 重新開機 | GB `PayloadTypeV2.SYSTEM_CONTROL_SET (0x98)`、`SonyProtocolImplV2.reboot()` |
| `98 00 06 01` 等 | 雙裝置連線（多點）設定 | GB `SonyProtocolImplV2.setConnectTwoDevices()` |
| `24 03 01` | 關機 | GB `PayloadTypeV2.POWER_SET (0x24)`、`SonyProtocolImplV2.powerOff()` |
| type `0x0E` 全部 | Table 2，含配對裝置管理（`t2::Peripheral*`） | MOS `libmdr/src/HeadphonesV2.cpp` |
| V1 `22 00 01`（以及 V1 的任何 `22 …`） | 關機 | GB `PayloadTypeV1.POWER_OFF (0x22)`、`SonyProtocolImplV1.powerOff()` |
| V1 `98 01 02 01` | 重新開機 | GB `SonyProtocolImplV1.reboot()` |
| V1 `84 …` | 降噪最佳化（會開始量測） | GB `PayloadTypeV1.NOISE_CANCELLING_OPTIMIZER_START` |
| V1 `f8 …` | 自動關機、按鍵、說話即對話等設定 | GB `PayloadTypeV1.AUTOMATIC_POWER_OFF_BUTTON_MODE_SET` |

注意：同一個指令碼 `0xF8` 在 V2 既用於「恢復原廠」(`f8 09 …`) 也用於一般系統設定 (`f8 01 …` 等)，所以白名單以**完整 payload 樣式**比對，而不是只看第一個 byte。

---

## 8. 與 A2DP 共存

Sony 控制通道是獨立的 RFCOMM 連線，與 A2DP 音訊串流是不同的 profile；Gadgetbridge 與官方 App 都是在耳機播放音樂時同時使用這個通道。**已在 Windows 實測（2026-10-24）**：播放音樂時連線、交握、讀電量都正常。

---

## 10. 頭部追蹤（不走 Sony 控制通道）

XM5 的頭部方向**不是**透過上面的 RFCOMM 協定傳送，而是一個獨立的 **HID 感測器**，遵循 Android 的「Head tracker HID protocol」。Sony 在 2023 年的韌體更新中為 XM5 加入這個功能。

| 項目 | 內容 | 出處 |
|---|---|---|
| HID 集合 | Usage Page `0x20`（Sensors）、Usage `0xE1`（Other: Custom） | [AOSP: Head tracker HID protocol](https://source.android.com/docs/core/interaction/sensors/head-tracker-hid-protocol) |
| 識別字串 | Sensor Description（`0x0308`）屬性為 `#AndroidHeadTracker#1.0` | 同上 |
| 啟動回報 | Feature：Reporting State（`0x0316`）設為 All Events（`0x0841`）、Power State（`0x0319`）設為 Full Power（`0x0851`）、Report Interval（`0x030E`）設為非零 | 同上 |
| 方向資料 | Input：Custom Value 1（`0x0544`），3 個值的旋轉向量（弧度，範圍 ±π）；Custom Value 2（`0x0545`）角速度；Custom Value 3（`0x0546`）參考座標重設計數器 | 同上 |
| 頭部座標軸 | X：左耳→右耳；Y：後腦→鼻子；Z：脖子→頭頂 | 同上 |
| Windows 上的節點 | 本機實測：XM5 的 Bluetooth HID 服務底下有一個「HID Custom Sensor」，但狀態為 Unknown（節點未建立），此時讀不到資料 | 本機 `Get-PnpDevice -Class Sensor`（2026-10-24） |
| 修復方式 | 用 `BluetoothSetServiceState` 重新啟用耳機的 HID 服務（記錄卡住時先停用再啟用）；若節點出現但無法啟動（代碼 10），改用 Windows 內建的 `input.inf` 通用 HID 驅動。需要系統管理員權限 | [NicholasSlattery/sony-head-tracker](https://github.com/NicholasSlattery/sony-head-tracker)（MIT）`src/bluetooth.cpp` `rebindBluetoothHid()`、`useGenericHidDriver()`；README「When the sensor won't show up」 |
| 回報頻率 | XM5 只提供 40 ms 間隔，約每秒 25 筆 | 同上 README |

**本機實測（2026-10-24）：成功。** 執行 `sonyxm head --repair`（系統管理員）後感測器出現，`sonyxm head --debug` 讀到資料：

| 項目 | 實測值 |
|---|---|
| Feature report 2（唯讀） | `#AndroidHeadTracker#1.0`，接著 16 bytes 的 Persistent Unique ID：8 個 `00`、`42 54`（"BT"）、耳機的藍牙位址，符合規格中「用藍牙位址對應音訊裝置」的格式 |
| Feature report 1（讀寫） | 初始全為 0；寫入 `01 03`，也就是 bit0 回報狀態 = All Events、bit1 電源 = Full Power、間隔欄位 = 0 |
| Report interval | 裝置宣告 40–41 ms，程式設為 40 ms |
| Input report | 14 bytes（report ID + 旋轉向量 3×16 bit + 角速度 3×16 bit + 計數器 8 bit） |
| 回報頻率 | 24.9 筆/秒 |
| 啟動時漂移 | 開始回報後約 1 秒內，roll 從 0 平滑漂移到約 −10° 後穩定，推測是耳機的姿態濾波器在收斂。因此程式在開始後約 1.5 秒自動歸零一次 |
| 中斷 | 測試中出現一次約 4 秒沒有資料，恢復後重設計數器改變，角度重新從 0 開始 |

⚠️ 角度的正負方向（例如向左轉時偏航是否為正）尚待使用者確認。

---

## 11. 其他型號：WH-1000XM4 與 WH-1000XM6

> ⚠️ 本專案只有 XM5。以下格式來自參考專案的程式碼與測試向量，程式照著實作。**XM6 已有使用者回報可以使用（2026-10-24）**，但沒有 `--debug` 紀錄，所以下面各項細節仍標為未確認；**XM4 完全未經實機測試**，App 與 `sonyxm connect` 會標示「尚未在此型號測試」。
> 這些指令都在白名單內（§7），而且寫入後都會讀回確認；耳機不認得的查詢只會被忽略（只回 ACK），程式會改試下一種或放棄該項。

### 11.1 認型號（`sonyxm/models.py`）

| 型號 | 藍牙名稱包含 | 預期協定 | NC 子型別（依序嘗試） | 電量查詢（依序嘗試） | 等化器 | 頭部追蹤 | 出處 |
|---|---|---|---|---|---|---|---|
| WH-1000XM4 | `WH-1000XM4` | V1 | `02` | `10 00` | 5 段 + Clear Bass | 無 | GB `coordinators/SonyWH1000XM4Coordinator.java`（`BatterySingle`、`WindNoiseReduction`、`EqualizerWithCustomBands`）；MOS `Base.h` 註解「V1: XM4s and older」 |
| WH-1000XM5 | `WH-1000XM5` | V2 | `17`、`15` | `22 00` | 5 段 + Clear Bass | 有 | §6（實機） |
| WH-1000XM6 | `WH-1000XM6` | V2 | `19`、`17` | `22 00`、`22 08` | 10 段 | 有 | MOS `HeadphonesV2.cpp`、`HeadphonesV2T1.cpp`、`docs/device-support/WH-1000XM6.md`；GB `SonyWH1000XM6Coordinator.java`（標為實驗性：「Battery, equalizer, and quite a few others probably not working」） |

規則：
- 名稱比對不分大小寫；`WF-1000XM5` 這類耳塞不會被認成耳罩。
- 配對了好幾副支援的耳機時，選目前連著電腦的那一副；都沒連或都連著則要求 `--address`。
- 交握回報的協定和表上不同時，以交握為準，改用該版本的通用設定（全部子型別、全部電量查詢都試）。

### 11.2 降噪／環境音

**V1（XM4），子型別 `02`。** 出處：GB `SonyProtocolImplV1.setAmbientSoundControl()`、`handleAmbientSoundControl()`；GB 測試 `SonyProtocolImplV1Test.setAmbientSoundControl()`（有風噪的那組向量）；PB `CommandSerializer::serializeNcAndAsmSetting()` 交叉驗證欄位順序。

| 方向 | payload |
|---|---|
| 查詢 | `66 02` |
| 回覆／通知（8 bytes） | `67`/`69` `02 <開關> <設定類型> <模式> <?> <人聲> <等級>`：開關 `00` 關、`01` 開；設定類型 `02` 時模式 `0` 環境音、`1` 風噪減低、`2` 降噪；設定類型 `00`（不支援風噪的機型）時模式 `0` 環境音、`1` 降噪 |
| 設定 | `68 02 <00 關\|11 開> 02 <模式> 01 <人聲> <等級>`；降噪與風噪減低時等級送 `00`（GB 的寫法） |

本專案多了第四個模式「風噪減低」（`sonyxm nc wind`、App 的 Wind 按鈕），只在 V1 提供。

**V2 子型別 `19`（XM6 的自動環境音）。** 出處：MOS `ProtocolV2T1.hpp` `NcAsmSetParamModeNcDualModeSwitchAsmSeamlessNa`、`NcAsmRetParamModeNcDualModeSwitchAsmSeamlessNa`、`NcAsmInquiredType::MODE_NC_ASM_DUAL_NC_MODE_SWITCH_AND_ASM_SEAMLESS_NA = 0x19`；MOS 在耳機支援 `..._NOISE_ADAPTATION` 功能時改用這個子型別（`HeadphonesV2.cpp`）。

| 方向 | payload |
|---|---|
| 查詢 | `66 19` |
| 回覆／通知（9 bytes） | 與 `17` 相同的前 7 bytes，再加 `<自動環境音 00\|01> <靈敏度 00 標準\|01 高\|02 低>` |
| 設定 | `68 19 01 <開> <環境音> <人聲> <等級> <自動> <靈敏度>` |

本專案開放「自動環境音」開關（`sonyxm ambient --auto`、App 的開關）；靈敏度不提供調整，寫入時沿用耳機回報的值。

### 11.3 電量

| 版本 | 查詢 | 回覆／通知 | 出處 |
|---|---|---|---|
| V1 | `10 00` | `11`/`13` `00 <等級> <充電>` | GB `PayloadTypeV1.BATTERY_LEVEL_*`、`SonyProtocolImplV1.getBattery()`、`handleBattery()`；GB 測試 `getBattery()` |
| V2，含門檻 | `22 08` | `23`/`25` `08 <等級> <充電> <門檻>` | MOS `PowerInquiredType::BATTERY_WITH_THRESHOLD = 0x08`、`PowerRetStatusBatteryThreshold` |

XM6 用哪一種不確定（MOS 依耳機宣告的功能表決定，GB 對 XM6 只送 `22 00` 且註明「probably not working」），所以程式先試 `22 00`，兩秒沒回覆再試 `22 08`，記住有回覆的那一種。App 中電量讀不到時不會斷線，只是不顯示電量。

### 11.4 等化器

**V1（XM4）。** 出處：GB `SonyProtocolImplV1.getEqualizer()`、`setEqualizerPreset()`、`setEqualizerCustomBands()`、`handleEqualizer()`；GB 測試 `SonyProtocolImplV1Test` 的同名測試（向量已放進本專案單元測試）。

| 方向 | payload |
|---|---|
| 查詢 | `56 01` |
| 回覆／通知 | `57`/`59` `01 <預設> 06 <Clear Bass> <5 段>`，各值 +10 |
| 切換預設 | `58 01 <預設> 00`（預設代碼同 §6.3） |
| 自訂頻段 | `58 01 ff 06 <6 個值>`：V1 沒有 Custom 1/2 之分，預設欄位固定填 `ff` |

**V2 10 段（XM6）。** 出處：MOS `HeadphonesV2T1.cpp` `HandleEqEbbParamT1()`（10 個值時沒有 Clear Bass，各值 −6..+6、+6 傳送）、`HeadphonesV2.cpp`（寫入同樣格式）、`client/Client.cpp`（頻率 31、63、125、250、500 Hz、1k、2k、4k、8k、16k Hz）。

| 方向 | payload |
|---|---|
| 回覆／通知 | `57`/`59` `00 <預設> 0a <10 個值>`，各值 +6 |
| 自訂頻段 | `58 00 <a0\|a1\|a2> 0a <10 個值>` |

XM6 的預設代碼（MOS `EqPresetId`、`docs/device-support/WH-1000XM6.md`：「Only Heavy/Clear/Hard/Soft (0x30–0x33), Custom (0xA0) and the user settings are accepted」）：`30` Heavy、`31` Clear、`32` Hard、`33` Soft，加上 `a0`/`a1`/`a2`。App 對 XM6 另外列出 `00`（關閉），⚠️ XM6 是否接受未確認；不接受時程式會讀回並回報「沒有套用」。

### 11.5 頭部追蹤

XM6 的頭部追蹤同樣走 Android Head Tracker HID（§10），NicholasSlattery/sony-head-tracker 的 README 把 XM6 列為支援，所以程式不需改動；XM4 沒有這個感測器，App 會顯示「此型號沒有頭部追蹤」。

---

## 9. 待實機確認清單

| # | 項目 | 何時確認 |
|---|---|---|
| 1 | ~~XM5 是否接受 V2 UUID、是否也有 V1~~ ✅ 只有 V2（見 §1） | 第 3 步 |
| 2 | ~~Python + ctypes `SOCKADDR_BTH`（帶 UUID）能否連上~~ ✅ 可以，約 1.2 秒連上 | 第 3 步 |
| 3 | 連線後是否需要延遲才送 init（目前等 0.5 秒，運作正常；不等是否也行未測） | 第 3/4 步 |
| 4 | ACK 與回覆封包的到達順序（init、電量實測：都是 ACK 先到；程式兩種順序都處理） | 第 4 步（`--debug`） |
| 5 | 充電狀態是否會出現 2/3 | 第 4 步 |
| 6 | ~~NC/ASM 子型別 `0x15` vs `0x17`~~ ✅ `0x17`（見 §6.2） | 第 4 步（先只送唯讀查詢） |
| 7 | ~~環境音強度 `0` 是否合法~~ ✅ 不合法，耳機會改成 1（見 §6.2） | 第 4 步 |
| 9 | 按耳機上的 NC/AMB 鍵時，是否會送 `69` 通知（`status --watch` 那次沒有印出任何變化；寫入時確定會送） | 待重測 |
| 10 | 頭部追蹤：~~修復後感測器是否出現、實際回報頻率~~ ✅（見 §10）；角度的正負方向仍待確認 | `sonyxm head` / App 的 3D 頭像 |
| 8 | ~~播放音樂時能否同時控制~~ ✅ 播放音樂時讀電量正常（使用者實測） | 第 4 步 |
| 11 | XM4：V1 服務能否連上、`10 00`／`66 02`／`56 01` 的回覆是否如 §11 所述；風噪減低、自訂頻段寫入後讀回的值 | 需要 XM4 的使用者 |
| 12 | XM6：~~整體能否使用~~ ✅ 使用者回報可以使用（2026-10-24）；細節（NC 子型別是 `19` 還是 `17`、電量用 `22 00` 還是 `22 08`、「關閉」預設是否被接受）尚無 `--debug` 紀錄 | 需要 XM6 使用者的 `--debug` 輸出 |

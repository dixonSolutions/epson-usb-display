# Epson "USB Display" (UD) protocol, USB 04b8:061b (EB-X04/X130/X300, VS340, ...)

This spec comes from static analysis of the Windows "USB Display" 1.6x binaries (EMP_PJCON.dll,
UDManager.dll, EMP_UDSA.exe) shipped on the projector's built-in `EPSON_PJ_UD` CD-ROM.

**Verified on hardware** (EB-series unit reporting platform 11 / "PGUD", 1024x768): capability
query, Y-mode connect, BUSY -> REDY -> unsolicited RESUME handshake, and full-frame 0xA0 band
updates at ~9.6 fps. Section 8 describes that unit; sections 5-6 (JPEG "C" mode) are untested. All addresses are VAs in the named
binary (image base 0x10000000 for the DLLs, 0x00400000 for EMP_UDSA.exe). Multi-byte values are
little-endian (LE) unless marked BE.

Terms: an "econ command" is the one-byte code Epson puts in CDB byte 2 (0x80, 0xA0, 0xC0, 0xC1,
0xC3, or 0x00 for reads). The "payload" is the data phase that goes with it.

---------------------------------------------------------------------------------------------------
## 0. Device discovery

* The projector enumerates as USB mass storage (a CD-ROM LUN) plus HID. The Windows service finds
  the right drive by its **volume label `EPSON_PJ_UD`** (EMP_UDSA.exe, `_strcmp(label,"EPSON_PJ_UD")`
  CUDFindDeviceMgr::VolumeLabelChk). It then opens `\\.\X:`
  (EMP_UDSA FUN_0040cfc0, CUSBCommunicationWinCore::OpenDevice).
* On Linux, open the matching `/dev/sgN` (or `/dev/srN` with SG_IO) and send the vendor CDBs below
  with SG_IO.

---------------------------------------------------------------------------------------------------
## 1. Transport: SCSI vendor command 0xD9

### 1.1 SCSI_PASS_THROUGH_DIRECT parameters (EMP_UDSA FUN_0040cc1b / EMP_PJCON FUN_1004e5c0, DeviceIOCore)
| field | value |
|---|---|
| IOCTL | 0x4D014 (IOCTL_SCSI_PASS_THROUGH_DIRECT) |
| PathId / TargetId / Lun | 0 / 1 / 0 (Windows addressing only; on Linux just use the sg node) |
| CdbLength | 12 |
| SenseInfoLength | 0x20 |
| DataIn | 1 = SCSI_IOCTL_DATA_IN (read) when the read flag is set, otherwise 0 = DATA_OUT |
| TimeOutValue | `this+0x18 / 1000` s. Default this+0x18 = 3000 ms, so **3 s** (FUN_0040cb8b) |
| error | ScsiStatus & 2 (CHECK CONDITION) gives -0x1F7. An IOCTL failure gives -0x1F6/-0x1F5 |

### 1.2 Standard CDB: "Send" (EMP_PJCON FUN_1004f290 = CUSBCommunication vtbl+0x20; same in EMP_UDSA FUN_0040c95b)
Signature: `Send(bool read, u8 econCmd, void *buf, u32 len)`
```
byte:  0    1    2        3      4      5      6      7    8  9  10 11
      D9   00   econCmd  len>>24 len>>16 len>>8 len&ff  45   00 00 00 00
```
* Bytes 3..6 hold the **transfer length, BE u32**. Byte 7 = 0x45 'E' means final/only transfer.
* The CDB then goes through the chunker (1.4).

### 1.3 Header-in-CDB variant, used only in "Y" connect type (EMP_PJCON FUN_1004f1d0 = vtbl+0x24; EMP_UDSA FUN_0040cae0)
Signature: `SendY(bool read, u8 econCmd, struct *s, void *buf, u32 len)`. The struct `s` is the
command item at +0xC (see §6.3):
```
byte: 0  1  2       3        4        5        6        7   8        9        10      11
     D9 00 econCmd s+4 hi   s+4 lo   s+6 hi   s+6 lo   45  s+8 hi   s+8 lo   s+0xA   00
```
* s+4 = u16 band top Y, s+6 = u16 band height, s+8 = u16 audio-block size (+5), s+0xA = u8
  "last band" flag. All BE in the CDB.
* The data length is **not** in the CDB. This path is **never chunked** and requires
  `len <= maxTransfer` (FUN_1004f420 / EMP_UDSA FUN_0040c9e2).

### 1.4 Chunking and pacing (EMP_PJCON FUN_1004f5c0, EMP_UDSA FUN_0040c7ed = CUSBCommunication::DeviceIO)
State: `maxTransfer` (this+4), `shortPacket` (this+8), `waitUs` (this+0xC). Defaults are
0xFFFF / 0x40 / 1000 (EMP_UDSA ctor FUN_0040c74b; EMP_PJCON CUSBCommunicationWin ctor FUN_1004ef20,
`param_1[6..8]=0xffff,0x40,1000`).
```
remaining = len; off = 0; tLast = now_us()
while remaining:
    chunk = min(maxTransfer, remaining)
    if WRITE:
        if shortPacket > 0 and chunk % shortPacket == 0: chunk = chunk/2 + 1   # avoid ZLP
        if remaining == maxTransfer + 1:                  chunk = maxTransfer/2
    remaining -= chunk
    cdb[3..6] = BE32(chunk)                 # per-chunk length, not the total
    cdb[7]    = 0x45 ('E') if remaining == 0 else 0x43 ('C')
    busy-wait until now_us() - tLast > waitUs            # QueryPerformanceCounter in µs
    SCSI(cdb, buf+off, chunk); tLast = now_us(); off += chunk
```
Bytes 0, 1, 2 and 8..11 of the CDB stay the same in every chunk.

**Values set at connect** (FUN_10070a20 @ 10070ed3..10070f1f, vtbl+8 = SetMaxTransferShortPacketSize
FUN_1004f350). They depend on the ClientIdentifier "platform" byte (capability tag 1, §3.2):
| platform | maxTransfer | shortPacket | waitUs |
|---|---|---|---|
| 4 | M | -1 (disabled) | 1000 |
| 5 or 8 | 0xFFFF | 0x40 | 0 |
| any other (**7 = known UD projectors**) | M | -1 | 0 |

M = FUN_10050110 = **0x10000**, or 0x20000 if the registry value DeviceHackFlags == 8 (Vista+).
Before connect (capability query) the defaults 0xFFFF/0x40/1000 apply. They only matter for
payloads over 64 KiB.

### 1.5 Where the CDB is built
EMP_PJCON builds the full 12-byte CDB. UD_DeviceIO (FUN_10050da0) copies `CDB(12) + data` into the
shared memory `Global\UDSA_DataShare`. The EMP_UDSA service (CUDSA::DeviceIO FUN_0040c415 ->
vtbl+0x18 chunker; DeviceIOForY FUN_0040c529 -> vtbl+0x1c no-chunk) sends it unchanged. The
in-process "Core" path (FUN_1004f5c0/FUN_1004f420) behaves the same way.

---------------------------------------------------------------------------------------------------
## 2. Econ commands summary

| dir | CDB byte2 | payload (data phase) | function |
|---|---|---|---|
| OUT | 0xC3 | `C3 00` (2 bytes) | SendCmdQueryCapability FUN_1006f8d0 (UDManager same) |
| OUT | 0xC1 | `C1 xx` (2 bytes). xx = 0x43 in Connect | SendCmdStatusQuery FUN_1006fd40 / FUN_1006f850 |
| OUT | 0xC0 | 42 bytes, `C0 'C'|'X'|'Y' ...` | SendCmdConnect FUN_10070a20 |
| OUT | 0xC0 | 42 bytes, `C0 44('D') 00*40` | SendCmdDisConnect FUN_10070770 |
| OUT | 0x80 | EPRD frame (picture / screen-format) | SendCmdImage FUN_1006fc10, SendCmdChangeFrameBuffer FUN_1006fac0 |
| OUT | 0xA0 | Y-type band data (CDB variant 1.3) | SendCmdImage when item type==2 |
| IN  | 0x00 | 256 bytes, then optional extra | ReceiveEconData FUN_1006f740 |

---------------------------------------------------------------------------------------------------
## 3. Reading projector->host messages (ReceiveEconData, FUN_1006f740; header parser FUN_1004f030)

1. Issue a READ: CDB `D9 00 00 00 00 01 00 45 00 00 00 00`, data-in, 256 bytes.
2. Parse:
   ```
   [0]    must be 0xFF. Otherwise "no message": return OK and ignore the buffer.
   [1]    bit7 = flag (returned to caller, not otherwise used); bits0-6 = message type
   [2..5] BE u32 N = payload length (payload starts at [6])
   ```
   `extra = N - 250`. If extra > 250 (N > 500) the parser returns -3 and the caller silently drops
   the message (FUN_1004f030 @1004f079..1004f08e). If N == 0, stop.
3. If extra > 0, issue a second READ (sub 0) of exactly `extra` bytes. Those bytes continue the
   payload.
4. Dispatch on type (AnalysisData FUN_1006feb0, base FUN_1006f6b0):

| type | meaning | payload |
|---|---|---|
| 1 | remote key | [6]=key code (7 = PageUp, 8 = PageDown), [7]=1 down / 0 up |
| 2 | ESC/VP21 text | logged only |
| 3 | message | ignored |
| 4 | status | ASCII `"REDY"` or `"BUSY"` at [6..9] |
| 5 | RFB server control | [6]=0x01, [7]: 0 = RESUME, 1 = PAUSE, 2 = STOP |
| 6 | capability reply | TLV blob at [6..] (§3.2) |
| 8 | IMBUFFER stats | u32 @[6], u32 @[10] (logged only) |

### 3.1 Connection state machine (AnalysisData)
* After SendCmdConnect, state = 4 (ECON_CONNECTING) (@10070f8e).
* "REDY" while state == 4:
  * If ProjectorFunction.PauseAndResume (§3.2 tag 8, v[0] bit2) is set, state = **8 CONNECTED_PAUSE**.
  * Otherwise Sleep(2000) and state = **9 CONNECTED**.
* Type 5 RESUME: state = 9 (and projection is enabled). PAUSE: state = 8. STOP: error, disconnect.
* ChangeConfiguration and the send loop only transmit when state == 9 (FUN_10053590 checks `== 9`).

### 3.2 Capability blob (TLV) — parser FUN_100872a0 (PJCON), FUN_10001c90 (UDManager)
Element encoding: `tag(1) len value`. `len` is **1 byte for tags 1..8** and **2 bytes LE for tag 0
and tags > 8**.

Tag 0 (AllCapability) is the container: `00 LL LL ver`. LL covers `ver` plus all following
elements. Tags:

| tag | class | value layout |
|---|---|---|
| 1 | ClientIdentifier (len 4) | [0]=platform (0 BERN, 1 KIRO, 2 UK, 3 ALAS, 4 JAPA, 5.., **7 = UD "NZUD"**), [1]=variation, [2..3] |
| 2 | (FUN_100828b0) | 6 bytes, e.g. `"NZUD\0\0"` |
| 3 | PanelResolution (len 6) | u16 LE width, u16 LE height, 2 bytes |
| 4 | FramebufferResolution | list of 6-byte entries {u8 flags (bit0 = 16bpp, bit3 = 32bpp, else 24), u8, u16 LE w, u16 LE h} (FUN_1007f430) |
| 5 | SupportRectEncoding | list of 22-byte entries (FUN_10052510/FUN_10052bd0): u8 RfbEncodingType, u8 SubEncodingType, u16 AlignX, u16 AlignY, u16 RectUnitW, u16 RectUnitH, u16 RectMinW, u16 RectMinH, u16 RectMaxW, u16 RectMaxH, u16 Thrput, u16 AlignPic (all LE) |
| 6 | SupportMovieEncoding (len 4) | |
| 7 | SupportAudioEncoding (len 10) | |
| 8 | ProjectorFunction (len 8) | see below |
| 9 | (FUN_10081fd0), 10 MultiPC, 11 ClientResolution, 12 ForwardCoordinatesviaNetwork, 13 PoolData | |

Encoding names (FUN_10052a10, key = sub<<8 | type): 0x0907 **JPEG**, 0x0B07 **EIC**, 0x0607
JPEGALIGNED, 0x0F07 JPEGEX, 0x0C07 LZO, 0x0E07 LZOEX, 0x0D07 RAWEX, 0x0707 RAWEXALIGNED,
0x0000 RAW32, 0x0100 RAW16, 0x0200 RAW24, 0x0006 ZLIB.

ProjectorFunction (tag 8) value v[0..7]. Parser @ case 8 of FUN_100872a0; names from the dump
function around line 121976:
* v[0]: b0 InterruptConnection, b1 DynamicResolutionChange, b2 **PauseAndResume** (=
  m_bSupportServerControl), b3 RetryConnection, b4 **RectWithHeader**, b5 RectWithoutHeader,
  b6 isRequiredSeparatedUpdateRectCommand, b7 UniDirectionalTCPConnect
* v[1]: b0 DES, b1 AES
* v[2]: b0 **StretchJPEG**, b1 ExProtocol
* v[3]: b0 BiDirTCPKeepAlive, b1 **RectScanlined**, b2 InteractivePenPositionCorrection,
  b3 ScreenType, b4 MPPInterruptConnectionToNP, b5 YConnectionExceptPanelSize, b6 QRConnect
* v[4..5]: u16 LE MaxRectDataSize. v[6..7]: unknown (seen `01 00`).

**Known UD capability blobs are built into EMP_PJCON** at 0x100d1f38 (as received) and 0x100d2238
(replacement). If a received blob matches one exactly, the replacement is used (FUN_100872a0 top).
The only difference is that **tag 5's length 0x28 is corrected to 0x2c**: the firmware reports the
wrong length and the list really holds 2×22 bytes. Replicate this fix.
```
Blob A (XGA, EB-X series / VS340 likely):
00 69 00 00                      tag0 len=0x69 ver=0
01 04 07 00 00 00                platform=7
02 06 4e 5a 55 44 00 00          "NZUD"
03 06 00 04 00 03 00 00          panel 1024x768
04 06 02 00 00 05 20 03          framebuffer entry flags=02, 1280x800
05 2c                            (firmware sends 05 28!)
   07 0b 0800 0000 0800 0800 1000 0800 0002 0002 0000 0000   EIC : align(8,0) unit 8x8  min 16x8  max 512x512
   07 09 0800 0000 0400 0000 2000 2000 0002 0002 0000 0000   JPEG: align(8,0) unit 4x0  min 32x32 max 512x512
06 04 00 00 00 00                no movie
07 0a 00*10                      no audio
08 08 16 00 00 00 00 00 01 00    v0=0x16: DynRes, PauseAndResume, RectWithHeader; no crypto, no StretchJPEG, not scanlined
Blob B (WXGA): same but panel `03 06 00 05 20 03 00 00` = 1280x800.
```
* In UDManager the "ProjectorType" is cap byte 12 of the reply, i.e. the tag-1 platform byte
  (FUN_1006f6b0: `cap+0xe4 = cap[0x114]`, with the payload copied to cap+0x10e).
* UNCERTAIN: FUN_1006f6b0 copies the extra bytes (payload > 250) to cap+0x10c rather than
  cap+0x10e+250. This looks like an Epson bug, and 108-byte blobs never hit it.

---------------------------------------------------------------------------------------------------
## 4. Session sequence

1. **Open** the device (§0).
2. **Capability** (UDManager CUDManager::GetCapability FUN_10012a20):
   * OUT `D9 00 C3 00 00 00 02 45 00 00 00 00` with payload `C3 00`.
   * Loop up to 200 times: READ (§3). If a type-6 message arrived, stop. Otherwise Sleep(100 ms).
   * Parse the TLV (§3.2). Decide the connect type from tag 8 (FUN_10068350):
     * if v[3].b1 RectScanlined: type 2 "Y" (CPictureControlCommand_XY);
     * else if RectWithHeader (v[0].b4, **or neither b4 nor b5 set**): type 0 "C"
       (CPictureControlCommand_C);
     * else: type 1 "X".
     * **Known UD blobs give type 0 = "C".** The rest of this spec describes C.
3. **Connect** (CProjectorUD::Connect FUN_100541a0 -> SendCmdConnect FUN_10070a20):
   * Set chunk parameters (§1.4).
   * OUT `D9 00 C0 00 00 00 2A 45 00 00 00 00` with this 42-byte payload (disassembly
     10070aa6..10070e54):
   ```
   off  size  value
   00   1     C0
   01   1     'C'(0x43) type0 | 'X'(0x58) type1 | 'Y'(0x59) type2
   02   2 LE  width   = FUN_10085f40 (see note)
   04   2 LE  height  = FUN_10085ed0
   06   1     bpp:   0x20 (C/X) or 0x18 (Y) when desktop is 24/32 bpp; 0x10 otherwise
   07   1     depth: 0x10 (constant in both branches, as coded)
   08   1     big-endian flag = 0
   09   1     true-colour = 1
   0A   2 LE  red max   (0x00FF, or 0x001F for 16 bpp)
   0C   2 LE  green max (0x00FF, or 0x003F)
   0E   2 LE  blue max  (0x00FF, or 0x001F)
   10   1     red shift   (0x10, or 0x0B)
   11   1     green shift (0x08, or 0x05)
   12   1     blue shift  0
   13   3     0
   16   4     client-resolution value from tag 11 (0 when tag 11 is absent)
   1A   4     audio: 01 00 <tag7 byte v[1]> ?? if audio is used, else 0 (send 0)
   1E   12    0
   ```
     Width/height note (FUN_10085f40/ed0): if platform <= 4, use the PanelResolution w/h. If
     platform >= 5 and panel == first FramebufferResolution entry, use that value. **Otherwise the
     code returns the constant 800 (0x320)**, independently for width and height (@10085f38).
     For Blob A this gives 800×800; Blob B gives 1280×800. UNCERTAIN whether the projector cares.
     Copying the exact values is safest.
     Bit depth (FUN_10085d90): for Blob A, 32-bpp desktop gives 0x20.
   * Then loop up to 100 times (Connect @FUN_100541a0):
     - OUT status query `D9 00 C1 00 00 00 02 45 ..` payload `C1 43`
     - READ (§3) and process
     - break when state is 8 or 9, else Sleep(100 ms)
   * Expect "REDY". Blob A has PauseAndResume, so state goes to 8. Keep polling READs until a
     type-5 message `01 00` (RESUME) arrives, which sets state 9. UNCERTAIN whether the projector
     sends RESUME on its own or only when the user selects the USB Display source.
4. **Screen format** (optional; CProjectorUD::SendScreenFormat FUN_10053050 -> ChangeConfiguration
   FUN_10053590 -> CreateScreenFormatCommandData FUN_100672c0 -> FUN_10066de0 -> SendCmdChangeFrameBuffer
   FUN_1006fac0):
   * Only sent in state 9. It is skipped unless forced (param_10) or v[3].b2 pen correction is
     set. It fires when the capture resolution changes (pending struct at CProjectorUD+0x2c4).
   * UNCERTAIN: whether the first frame needs it. Sending it once after reaching state 9 matches
     the "forced" path. If platform <= 4 and bpp < 16, bpp is forced to 16.
   * OUT cmd 0x80, CDB `D9 00 80 00 00 00 2C 45 ..`, 44 bytes:
   ```
   00  "EPRD"  45 50 52 44
   04  "0600"  30 36 30 30
   08  u32 0
   0C  u32 0
   10  u32 LE body length = 0x18      <-- LE here (mov [edi+0x10],ecx @10066ec5), unlike picture frames
   14  C8 00 00 00
   18  u16 BE width, u16 BE height
   1C  bpp (0x20 for C/X, 0x18 for Y; 0x10 for 16bpp), depth (= bpp for 24/32; 0x10), 00 (BE flag), 01 (true colour)
   20  u16 BE rmax (00 FF | 00 1F), u16 BE gmax (00 FF | 00 3F), u16 BE bmax (00 FF | 00 1F)
   26  rshift (10 | 0B), gshift (08 | 05), bshift 00, pad 00 00 00
   ```
     With pen correction (tag8 v[3].b2) the body is 0x2E bytes: byte0 = 0xCC, the same
     w/h/pixfmt, then 6 × u16 BE work-area values and zero padding. The C type always sends the
     EPRD header (param_7 = 1).
5. **Frames** (§5). The send loop is CProjectorUD::Send FUN_10053230 -> Send1Frame FUN_100537e0.
6. **Keep-alive / status**: CheckStatus FUN_10053180 runs in the send loop. Whenever **more than
   100 ms** have passed since the last one, do a READ (§3). No command is written for keep-alive;
   the periodic read is the only thing. Handle types 4/5 (PAUSE: stop sending; RESUME: continue;
   STOP: tear down).
7. **Disconnect** (CProjectorUD::Disconnect -> FUN_10070770): OUT cmd 0xC0, 42 bytes
   `C0 44 00 … 00`, then close the device.

---------------------------------------------------------------------------------------------------
## 5. Picture frame (connect type "C") — cmd 0x80

Built by CPictureControlCommand_C::CreateCommands FUN_100686a0 -> FetchPictureCommand FUN_10068560.
Serialized by FUN_10068810 (EPRD header), FUN_10068ec0 (FramebufferUpdate), FUN_10068c70/d70/ac0
(rect headers) and FUN_10068880 (rect data). Sent by SendCmdImage FUN_1006fc10 as
`Send(write, 0x80, buf, total)`, chunked per §1.4. There is **one 0x80 command per frame**, and
Send1Frame sends every item in the list.

```
EPRD header (20 bytes, FUN_10068810):
00  45 50 52 44          "EPRD"
04  30 36 30 30          "0600"
08  u32  = command-object+0x14 (always 0 in the C class: ctor FUN_10067ca0; UNCERTAIN whether anything sets it)
0C  u32  0
10  u32 **BE** L = length of everything after this header
FramebufferUpdate (4 bytes, FUN_10068ec0):
14  00                    message type 0
15  00                    padding
16  u16 BE nRects
Per rect: header, then data
```
L = 4 + Σ(rectHeaderLen + dataLen) (FUN_100690b0). Total transfer = 20 + L.

Rect header types (the rect object's field [0], chosen in CPictureData::DoEncoding FUN_1005ce60 /
FUN_1005cc40, compressor table FUN_1005a720):

| type | compressor | header | layout |
|---|---|---|---|
| 0 | **JPEG** (CPictureCompressorJPEGWin) | 16 B | `x y w h` (BE16 each), `00 00 00 07` (Tight), **ctrl 0x90**, compact-len 3 B |
| 5 | LZO | 16 B | same, ctrl 0xC0 |
| 6 | EIC (eIC.dll) | 16 B | same, ctrl 0xB0 |
| 1/2 | RAW32/RAW16 | 12 B | `x y w h` BE16, encoding `00 00 00 00` |
| 3 | RAWEX | 28 B | `x y w h`, `00 00 00 07`, ctrl 0xD0 (0x70 aligned), bpp 0x18/0x10, zeros, BE32 len |

Where (FUN_10068c70):
* x = left, y = top, w = right-left, h = bottom-top (exclusive right/bottom).
* The ctrl byte is (SubEncodingType << 4), matching tag 5: JPEG sub 9 -> 0x90, EIC sub 0xB -> 0xB0.
* Compact length (Tight style, but **always 3 bytes**): `b0 = (len & 0x7F)|0x80`,
  `b1 = ((len>>7) & 0x7F)|0x80`, `b2 = (len>>14) & 0xFF`.

JPEG rect data is a complete baseline JFIF stream. Encoder settings (CPictureCompressorJPEGWin
vtbl 0x100c1bcc: init FUN_10069820 sets quality 0x50; encode FUN_100697b0 -> FUN_1006d580, libjpeg
v6b-style API with struct size 0x168):
* input 24-bit (the code converts BGR to RGB), `in_color_space = JCS_RGB`, 3 components
* `jpeg_set_defaults`, `jpeg_set_quality(80, force_baseline=TRUE)`, `jpeg_start_compress(TRUE)`
* libjpeg defaults therefore apply: YCbCr **4:2:0**, standard Huffman tables, no restart markers,
  JFIF APP0. Quality 80 is the default and may change with the "performance" setting (UNCERTAIN).
* Compact length = JPEG byte count, and the data is exactly those bytes (FUN_1005a850 appends the
  compressor output and records its size; UNCERTAIN whether any prefix is included, but none is
  written for JPEG).

Rectangle constraints come from the tag-5 entry for the chosen encoding. For JPEG on Blob A:
RectMin 32×32, RectMax **512×512**, Align (8,0), RectUnit 4×0. A 1024×768 panel therefore needs
tiles, e.g. 2×2 tiles of 512×384. The Windows client sends only changed blocks (partial updates
are allowed and normal). A full frame is simply all tiles. Capture size = panel resolution
(UDManager stores tag3 w/h at +0x960/+0x964, which drive the capture size in FUN_10056e90/FUN_1007d8d0).
**No StretchJPEG** on Blob A, so send JPEGs at the rectangle's real size. No padding or
alignment of the EPRD buffer is needed beyond the chunking rules.

Encryption: CProjector::GetEncryptType FUN_10056a00 returns 0 unless encryption is requested
**and** tag8 v[1] has DES/AES. The UD command builders contain no encryption code. **UD mode does
not encrypt.**

---------------------------------------------------------------------------------------------------
## 6. Other connect types (not used by the known UD blobs)

### 6.1 X (RectWithoutHeader only)
CPictureControlCommand_X (vtbl 0x100c1c80): CreateCommands FUN_100678f0 has separate
audio/picture/update commands (FUN_10067310/FUN_10067710/FUN_10067410). Not analysed.

### 6.2 Y / XY (RectScanlined)
CPictureControlCommand_XY (ctor FUN_10067d20) wraps X and Y. Y::CreateCommands FUN_10067df0 builds
one item per scanline band:
* item+0xC type=2, +0x10 u16 top, +0x12 u16 height, +0x14 u16 audioLen+5, +0x16 u8 last-band.
* The data is the band, padded to a 32-byte multiple, followed by an optional audio block
  `C9 <len 4 bytes>` (at most 4000).
* Each item is sent with cmd **0xA0** using CDB variant 1.3, unchunked (len <= maxTransfer).

---------------------------------------------------------------------------------------------------
## 7. Open items / UNCERTAIN
1. The connect width/height fallback of 800 (§4.3). Try the exact code value first. If the image
   is wrong, try the panel size.
2. Whether RESUME (type 5 `01 00`) arrives unprompted. Also whether the ScreenFormat (0xC8)
   command is needed before the first frame. Resolve by USB-capturing the Windows client
   (usbmon/Wireshark on a Windows VM with USB passthrough).
3. EPRD bytes 8..15 in picture frames are 0 in every code path I found.
4. The actual capability blob of the user's unit. Read it with the §4.2 query. If it matches
   Blob A/B, everything above applies directly.
5. Timeout value changes (UD_SetTimeOutValue FUN_10050b30) not traced; 3 s default assumed.

---------------------------------------------------------------------------------------------------
## 8. Y mode (platform 11 / "PGUD") — verified on hardware

Capability reply read from the real unit, re-parsed with the §3.2 rules:
```
00 53 00 00                 tag0 len 0x53, ver 0
01 04 0b 00 00 00           platform 11
02 06 50 47 55 44 00 00     "PGUD"
03 06 00 04 00 03 00 00     panel 1024x768
04 06 02 00 00 04 00 03     fb entry: flags 0x02, 1024x768
05 16 07 07 2000 0000 2000 2000 3000 3000 8000 a000 0000 0800
      RAWEXALIGNED (0x0707): Align(32,0) Unit 32x32 Min 48x48 Max 128x160 Thrput 0 AlignPic 8
07 0a 01 00 01 0c 00 00 00 00 00 00   audio supported (not needed)
08 08 66 00 00 22 00 00 01 00          v0=0x66 DynRes|PauseAndResume|RectWithoutHeader|SeparatedUpdateRect
                                       v3=0x22 RectScanlined|YConnectionExceptPanelSize ; MaxRectDataSize 0
0b 03 00 00 07 0a           ClientResolution: flags 0x00 (NOT supported), resolution IDs 0x07,0x0a
```
The tag-5 entry's last two u16 are Thrput=0 and **AlignPic=8**, not "thrput 0x0a". Tag 5's length
here (0x16) is correct, so no fix-up is needed. There is no tag 6.

### 8.1 Command object selection
FUN_10068350: tag8 v[3].b1 (RectScanlined) is set, so the code builds **CPictureControlCommand_XY**
(ctor FUN_10067d20, vtbl 0x100c1d04; ConnectType +4 = 2, so the connect byte is 'Y').
XY holds both:
* an **X** object (+0x18, vtbl 0x100c1c80). Setup: X+0x15 = v0.b6 SeparatedUpdateRect = 1;
  X+0x14 = FUN_1007d120(tag5,7,7) = RAWEXALIGNED supported = 1; X+0x16 = FUN_1007d6f0 =
  AlignPic = 8;
* a **Y** object (+0x1c, ctor FUN_10067b50, vtbl 0x100c1cd8).

XY picks one of them **per frame**. XY::SetPictureList FUN_100671d0 selects Y (flag +0x14 = 1,
type 2) when `pictureList+0x2c == 1`, otherwise X (type 1). All other XY methods
(FUN_10066c80 CreateCommands etc.) forward to the selected object.
`pictureList+0x2c` = CPictureData+0x1E4 (DoEncoding @1005d189). The partitioner FUN_1005ea00
sets it:
* **Y (bands)** when scanline mode is enabled (CPictureData+0x1D8) and FUN_1005bac0 finds a dirty
  rect with `(w > RectMaxW(128) || h > RectMaxH(160)) && (f1f8 || w > 896 || h < 257) &&
  (f1f8 != 1 || w > 1344)`. Bands are produced by FUN_1005d8b0 and the flag is set to 1.
* **X (rects)** otherwise: FUN_1005e360 produces small rectangles and the flag is set to 0.
  X frames go through SendCmdImage's 0x80 path (type != 2). X::CreateCommands FUN_100678f0 emits
  separate audio/picture/update commands (FUN_10067310/10067710/10067410), because
  SeparatedUpdateRect = 1. **Not decoded.**

Scanline enable (CPictureData::Initialize FUN_1005ec60 @ +0x1d8): `+0x1D8 = RectScanlined`
(FUN_1008d220). It is then cleared if the capture size (+0x13C/+0x140) differs from the size
returned by FUN_1008d9c0/FUN_1008d850 **and** YConnectionExceptPanelSize (FUN_1008d180, v3.b5)
is 0. **So YConnectionExceptPanelSize = 1 means Y bands are still allowed when the sent image
size is not the panel size.**

**Recommendation for a Linux client:** send every update as full-width Y bands (§8.4). A full
frame is always "large", and the Windows client itself would pick Y for it. Implementing X is
unnecessary unless you want small partial updates. UNCERTAIN: whether the projector needs an X
command before Y. No code path requires one.

### 8.2 Connect payload (SendCmdConnect FUN_10070a20), exact bytes for this unit
* width = FUN_10085f40: platform 11 >= 5 and panel w (1024) == fb entry w (1024), so **1024**.
* height = FUN_10085ed0: 768 == 768, so **768**.
* bit depth = FUN_10085d90: platform >= 5 and panel == fb, so it uses the fb entry flags 0x02
  (bit0 = 0, bit3 = 0) and returns **24** (0x18), whatever the desktop depth. The 24/32 branch
  with type 2 gives bpp = 0x18. Depth byte = 0x10 (constant, as in §4).
* 0x16..0x19: only filled when `[edi+0x28]==2 && tag11.flags&1` (@10070b21..10070b46). Here
  tag11 flags = 0x00, so **0**. The IDs 07/0a are ignored.
* 0x1A..0x1D: audio block only when audio is used (param_3). Unused means **0**.
```
C0 59 00 04 00 03 18 10 00 01 FF 00 FF 00 FF 00 10 08 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```
CDB: `D9 00 C0 00 00 00 2A 45 00 00 00 00`.

Chunk parameters (§1.4, @10070eea..10070f1f): platform 11 is not 4, 5 or 8, so
`SetMaxTransferShortPacketSize(M, -1, 0)` with **M = 0x10000** (0x20000 only if the Windows
registry DeviceHackFlags == 8). **Confirmed.** Status query and handshake are as in §4.3 (`C1 43`;
REDY gives state 8 because PauseAndResume = 1; wait for type 5 `01 00` RESUME to reach state 9).

### 8.3 Screen format in Y mode
XY vtbl+0x1c = FUN_100670c0 calls FUN_10066de0 with **param_7 = 0**, so **no EPRD header**.
Payload = the 24-byte `C8 00 00 00 | W BE | H BE | 18 18 00 01 | 00FF 00FF 00FF | 10 08 00 | 00 00 00`
(bpp = depth = 0x18 because type == 2 @10066f3e). It is sent as normal cmd 0x80
(SendCmdChangeFrameBuffer FUN_1006fac0), CDB `D9 00 80 00 00 00 18 45 ..`.
It is only sent from ChangeConfiguration FUN_10053590 when state == 9 **and** (forced or
v3.b2 pen correction). v3 = 0x22 has no b2, so it is only sent on a forced resolution change.
**Not required before the first band** by any code path I found (UNCERTAIN; harmless to send).

### 8.4 Band generation (FUN_1005d8b0, "y-adjuster")
* Rows touched by any dirty rect are marked. Contiguous runs become rects
  `{left=0, top=y0, right=W, bottom=y1}`: **bands are always full width** (W = CPictureData+0x144,
  the send width, i.e. 1024).
* Each run is then split by a CPRangeAdjuster built as
  `FUN_1008ffb0(x: align0,unit0,min=W,max=W,limit=W ; y: align2,unit2,min2,max=Hmax,limit=H)`.
  Hmax is:
  * **20 lines** if W < 1280 (so 20 for 1024),
  * 16 if W == 1280,
  * 8 if W > 1280 (@1005db14..1005db39: `W > 0x500 ? 8 : (W != 0x500)*4 + 0x10`).
* This keeps a band at 1024·20·3 = 61440 bytes, which is ≤ 0x10000 (the 0xA0 transfer is never
  chunked and is silently dropped if len > maxTransfer, FUN_1004f420).
* Band top/height are even (granularity 2). UNCERTAIN: the exact adjuster semantics
  (FUN_100772b0 args are register-passed). A full frame of 768 lines = 38 bands of 20 + 1 band
  of 8.
* The tag-5 Unit 32x32 / Min 48x48 / Max 128x160 / Align 32 values apply to **X rects** and to
  the Y/X decision, **not** to Y bands.

### 8.5 0xA0 band command (Y::CreateCommands FUN_10067df0, SendCmdImage FUN_1006fc10 -> FUN_1004f1d0)
The encoder for this unit: tag5 0x0707 (RAWEXALIGNED) maps to encode type 3
(FUN_1005aea0 @0x707 returns 3), compressor RAWEX (FUN_1005a720 case 3 -> +0x38
CPictureCompressorRAWEXWin, or RAWEX16 if the encoder +0x1c flag is set, which needs 16-bpp
capture).
RAWEX::Compress FUN_10069280 is a plain `memcpy(w*h*3)` and requires 24 bpp. There is **no rect
header, no EPRD header, no Tight header** in Y mode. The data phase is the raw pixels.

One 0xA0 command per band:
```
CDB:  D9 00 A0  Ytop_hi Ytop_lo  H_hi H_lo  45  A_hi A_lo  LAST  00
data: W*H*3 bytes of pixels [ + audio trailer, only if audio is enabled ]
```
* Ytop = band top (item+0x10 = rect.top), H = band height (item+0x12 = bottom-top), both BE16.
  There are no X/width fields: bands are implicitly full width W (as announced in connect).
* A = item+0x14 = audioLen+5 when an audio chunk is attached, **otherwise 0** (the item is zeroed
  and only written when local_34 != 0).
* LAST = item+0x16 (@10068089) = 1 on the **last band of this update** (index+1 >= band count), else 0.
* Transfer length = W*H*3 exactly. It is **not** chunked and must be ≤ maxTransfer (0x10000).
  There is no 'C'/'E' chunk marker change; byte 7 stays 0x45.
* **Pixel format:** 24 bpp, 3 bytes per pixel in Windows DIB order **B,G,R**. Rows are
  **top-down** and packed: stride = W*3 = 3072, no row padding. Sources: the capture/resize
  surfaces are top-down 24-bit BI_RGB DIBs (FUN_10075520: biHeight = -h), the block copy
  FUN_1005b280 packs rows at W*bytesPerPixel, and RAWEX does a straight memcpy with no swap.
  UNCERTAIN on R/B order only in the sense that I found no swap. If colours look wrong, swap.
* Audio trailer (only with audio; DO NOT send otherwise): pad the pixel data with
  `(32 - len%32) % 32` bytes, then `C9`, then **LE32** audio length (≤ 3995), then the audio bytes.
  CDB A = audioLen + 5. Without audio there is no padding and no C9 (@10067ec1..10067f02: `mov byte [esp+0x38],0xC9; mov dword [esp+0x39],len` = LE32;
  audio block capped at 0xFA0 incl. 5-byte header).
* Optional debug border (CPictureData+0x1D0, global DAT_100d64a8): FUN_1005b110 paints each
  block's outer pixels black. Normally off.

### 8.6 Pacing / flow control
* Send loop CProjectorUD::Send FUN_10053230: per iteration, CheckStatus (a READ only if more than
  100 ms have passed since the last one), then Send1Frame FUN_100537e0. Send1Frame calls
  CreateCommands repeatedly; Y yields **one band per call** and advances its index (FUN_10067df0
  @+0xc). All bands of an update go out back-to-back **without waiting for any read or ack**.
* IMBUFFER (type 8) messages are only logged (AnalysisData case 8). There is no credit-based flow
  control.
* Pause (type 5 `01 01`) stops sending until RESUME. STOP ends the session.
* Recommended cadence: after each full frame (39 bands), do one 256-byte READ when 100 ms have
  passed, then send the next frame. The per-band SCSI completion is the only back-pressure.

### 8.7 Minimal Linux client for this unit
1. SG_IO READ-capable device (label EPSON_PJ_UD). Optional capability query `C3 00`, then poll
   READ (type 6).
2. Connect: payload from §8.2.
3. Loop `C1 43` + READ until REDY. Then poll READ until type 5 `01 00` (RESUME). UNCERTAIN whether
   it arrives by itself.
4. (Optional) screen format §8.3.
5. For each frame: for y in 0..768 step 20: 0xA0 with the CDB above, H = min(20, 768-y), A = 0,
   LAST = (y+H == 768), data = BGR24 rows y..y+H-1 (1024*H*3 bytes). Between frames, READ every
   ≥100 ms and handle PAUSE/RESUME/STOP.
6. Disconnect `C0 44 00*40`.

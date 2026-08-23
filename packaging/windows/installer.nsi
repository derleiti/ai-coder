; ai-coder Windows Installer — NSIS
; Desktop + Start Menu shortcuts for CLI and GUI.
; The EnVar plugin is staged by the GitHub Actions workflow.

Unicode True

!define APP_NAME "ai-coder"
!define APP_EXE  "aicoder.exe"
!define INSTALL_DIR "$PROGRAMFILES64\ai-coder"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder"

Name "${APP_NAME} ${VERSION}"
OutFile "aicoder-${VERSION}-setup.exe"
InstallDir "${INSTALL_DIR}"
InstallDirRegKey HKLM "Software\ai-coder" "Install_Dir"
RequestExecutionLevel admin
ShowInstDetails show
ShowUninstDetails show

; --- Pages ---
Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

; --- Install ---
Section "ai-coder (required)"
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; Keep the installed filename stable even if EXE_PATH changes later.
  File /oname=${APP_EXE} "${EXE_PATH}"

  ; Add install directory to the system PATH.
  EnVar::SetHKLM
  EnVar::AddValue "PATH" "$INSTDIR"
  Pop $0
  StrCmp $0 "0" path_added
  DetailPrint "Warning: PATH update returned EnVar status $0"
path_added:

  ; Write the uninstaller before creating its shortcut.
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Start Menu shortcuts
  CreateDirectory "$SMPROGRAMS\ai-coder"
  CreateShortcut "$SMPROGRAMS\ai-coder\ai-coder Terminal.lnk" \
    "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0 SW_SHOWNORMAL
  CreateShortcut "$SMPROGRAMS\ai-coder\ai-coder GUI.lnk" \
    "$INSTDIR\${APP_EXE}" "gui" "$INSTDIR\${APP_EXE}" 0 SW_SHOWNORMAL
  CreateShortcut "$SMPROGRAMS\ai-coder\Uninstall.lnk" \
    "$INSTDIR\uninstall.exe" "" "" 0

  ; Desktop shortcuts
  CreateShortcut "$DESKTOP\ai-coder GUI.lnk" \
    "$INSTDIR\${APP_EXE}" "gui" "$INSTDIR\${APP_EXE}" 0 SW_SHOWNORMAL
  CreateShortcut "$DESKTOP\ai-coder Terminal.lnk" \
    "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0 SW_SHOWNORMAL

  ; Registry
  WriteRegStr HKLM "Software\ai-coder" "Install_Dir" "$INSTDIR"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayName" "ai-coder"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "Publisher" "AILinux / Markus Leitermann"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "URLInfoAbout" "https://ailinux.me"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

; --- Uninstall ---
Section "Uninstall"
  EnVar::SetHKLM
  EnVar::DeleteValue "PATH" "$INSTDIR"
  Pop $0
  StrCmp $0 "0" path_removed
  DetailPrint "Warning: PATH cleanup returned EnVar status $0"
path_removed:

  Delete "$SMPROGRAMS\ai-coder\ai-coder Terminal.lnk"
  Delete "$SMPROGRAMS\ai-coder\ai-coder GUI.lnk"
  Delete "$SMPROGRAMS\ai-coder\Uninstall.lnk"
  ; Remove the old localized shortcut too, if present from an earlier build.
  Delete "$SMPROGRAMS\ai-coder\Deinstallieren.lnk"
  RMDir "$SMPROGRAMS\ai-coder"

  Delete "$DESKTOP\ai-coder GUI.lnk"
  Delete "$DESKTOP\ai-coder Terminal.lnk"

  DeleteRegKey HKLM "${UNINSTALL_KEY}"
  DeleteRegKey HKLM "Software\ai-coder"

  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd

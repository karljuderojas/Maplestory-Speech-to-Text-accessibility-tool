@echo off
rem Runs the build with automatic GPU detection and saves all output to build_log.txt
echo Building Voice Chat... (output is going to build_log.txt in this folder)
call "%~dp0Build VoiceChat.bat" auto <nul > "%~dp0build_log.txt" 2>&1
echo Finished. See build_log.txt

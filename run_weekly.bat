@echo off
REM הרצה מקומית במחשב (חלופה ל-GitHub Actions אם האתרים חוסמים גישה מחו"ל)
cd /d "%~dp0"
python compare.py
git add docs
git commit -m "weekly basket update"
git push

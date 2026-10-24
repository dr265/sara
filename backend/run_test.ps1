cd $PSScriptRoot
python -m pytest tests/test_sara_bots_saramuse.py -v --tb=short 2>&1 | Out-File -FilePath C:\Users\HP\sara-ai-platform\backend\test_out.txt -Encoding UTF8

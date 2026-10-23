content = open("app.py").read()
old = "CSV_FILE = \"Report_7887_22-10-2026-02-53-52.csv\""
new = "import glob\nCSV_FILES = sorted(glob.glob(\"Report_*.csv\"), key=lambda x: os.path.getmtime(x))\nCSV_FILE = CSV_FILES[-1] if CSV_FILES else \"Report_7887_22-10-2026-02-53-52.csv\"\nprint(f\"Using CSV: {CSV_FILE}\")"
if old in content:
    content = content.replace(old, new)
    open("app.py","w",encoding="utf-8").write(content)
    print("done")
else:
    print("not found")

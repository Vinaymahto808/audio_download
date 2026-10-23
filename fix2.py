content=open("app.py").read()
old="demo.launch(server_name=\"127.0.0.1\", server_port=7860, share=False)"
new="demo.launch(server_name=\"127.0.0.1\", server_port=7861, share=False)"
if old in content:
    open("app.py","w",encoding="utf-8").write(content.replace(old,new))
    print("ok")
else:
    print("no")

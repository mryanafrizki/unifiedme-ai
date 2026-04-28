# MCP Test Prompts for OpenCode TUI

Copy-paste these prompts to test all MCP features via `gl-claude-opus-4-7`.

---

## 1. Basic — List & Read (warm up)

```
list all files in the workspace, then read hello.txt if it exists
```

## 2. Write + Edit + Verify

```
create a file called "notes.md" with 3 bullet points about AI coding assistants. then edit it to add a 4th bullet point. then read it back to confirm.
```

## 3. Multi-file Project

```
create a simple calculator web app with 3 separate files:
- calculator.html (the page)
- calculator.css (styling, dark theme)
- calculator.js (logic: add, subtract, multiply, divide)
no emoji. clean code.
```

## 4. Bash + Python Execution

```
create a python script called "fibonacci.py" that prints the first 20 fibonacci numbers. then run it with bash and show the output.
```

## 5. Glob + Grep (Search)

```
find all .html files in the workspace, then search for the word "calculator" across all files. show results.
```

## 6. Image Generation + Download to Workspace

```
generate an image of a futuristic city skyline at sunset, cyberpunk style. save it to workspace as "cyberpunk_city.png" using download_image with the gl:// URL.
```

## 7. Full Stack — CRUD Todo App

```
build a complete todo app with these files:
- todo.html (single page app)
- todo.css (modern dark theme, no emoji)
- todo.js (full CRUD: add, edit, delete, mark complete, persist to localStorage)
make it production quality. then list all files to confirm.
```

## 8. Advanced — Multi-step with Context

```
1. read all .py files in workspace
2. create a new file "summary.md" that lists every python file with a one-line description of what it does
3. create a bash script "run_all.sh" that runs each python file
4. run the bash script and show output
```

## 9. Image Edit Workflow

```
generate a simple logo for a company called "NexaCode" - minimalist, blue and white. save to workspace as "nexacode_logo.png". then create an HTML page "brand.html" that displays the logo with the company name below it.
```

## 10. Stress Test — Large File

```
create a file "data.json" with a JSON array of 50 fake user objects. each user has: id, name, email, age, city, role. make the data realistic. then use grep to find all users from "Jakarta". then use bash to count total lines in the file.
```

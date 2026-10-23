# Fast smoke test (~20 requests, well under a minute). Run it after every change:
#   hurl --test --variable base=http://127.0.0.1:8000 --variable email=<admin email> --variable password=<admin password> scripts/qa/smoke.hurl
# Creates and removes its own project and work item; leaves the data as it found it.

GET {{base}}/api/health
HTTP 200

POST {{base}}/api/auth/login
{"email": "{{email}}", "password": "{{password}}"}
HTTP 200

GET {{base}}/api/auth/me
HTTP 200
[Asserts]
jsonpath "$.email" == "{{email}}"

POST {{base}}/api/projects
{"name": "qa-smoke-{{newUuid}}", "agile_enabled": true}
HTTP 201
[Captures]
project_id: jsonpath "$.id"

GET {{base}}/api/projects
HTTP 200

POST {{base}}/api/bugs
{"project_id": {{project_id}}, "title": "smoke item"}
HTTP 201
[Captures]
bug_id: jsonpath "$.id"

GET {{base}}/api/bugs/{{bug_id}}
HTTP 200
[Asserts]
jsonpath "$.title" == "smoke item"

POST {{base}}/api/bugs/{{bug_id}}/comments
{"body": "smoke comment"}
HTTP 201

POST {{base}}/api/bugs/{{bug_id}}/attachments
[MultipartFormData]
file: file,smoke-attachment.txt; text/plain
HTTP 201

GET {{base}}/api/bugs?q=smoke
HTTP 200

GET {{base}}/api/agile/boards?project_id={{project_id}}
HTTP 200
[Captures]
board_id: jsonpath "$[0].id"

GET {{base}}/api/agile/sprints?board_id={{board_id}}
HTTP 200

GET {{base}}/api/agile/reports/velocity?board_id={{board_id}}
HTTP 200

GET {{base}}/api/stats
HTTP 200

DELETE {{base}}/api/bugs/{{bug_id}}
HTTP 200

DELETE {{base}}/api/projects/{{project_id}}
HTTP 200

POST {{base}}/api/auth/logout
HTTP *

GET {{base}}/api/auth/me
HTTP 401

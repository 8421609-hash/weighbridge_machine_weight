# Weighbridge Management System

A professional **Python-based Weighbridge Management System** designed to manage real-time truck weighing operations, Entry and Exit transactions, offline data storage, automatic backups, audit logging, live weight monitoring, and bulk synchronization with ERP Server.

The application communicates with physical weighbridge machine through serial/COM port and provides desktop-based interface for managing the complete truck weighing workflow.

##Key Features

* Real-time weight reading from a physical weighbridge
* Serial / COM port communication
* Automatic COM port configuration
* Separate Truck Entry and Exit transactions
* Duplicate active truck protection
* Offline-first architecture using SQLite
* Automatic database initialization and migration
* Local audit logging
* Continuous live weight logging
* Automatic database backups
* Configurable API endpoints
* Dynamic GET API for fetching active/pending trucks
* Dynamic POST API for server synchronization
* Token-based API authentication
* Bulk synchronization of unsynced records
* Sync status tracking
* Internet and server availability checking
* Remote Exit / multi-gate workflow support
* Searchable and filterable truck records
* PySide6 desktop user interface
* Background threads for weight reading and API operations
* Automatic error and event logging

---

## System Architecture

```text
                     WEIGHBRIDGE MACHINE
                             │
                             ▼
                      Serial / COM Port
                             │
                             ▼
                 ┌───────────────────────┐
                 │   weighbridge.py      │
                 │      script.py        │
                 └───────────────────────┘
                             │
                             ▼
                        LIVE WEIGHT
                             │
                             ▼
                 ┌───────────────────────┐
                 │       main.py         │
                 │    PySide6 Desktop    │
                 │       Application     │
                 └───────────────────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
           ENTRY            EXIT         SETTINGS
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                 ┌───────────────────────┐
                 │     database.py       │
                 │     SQLite Storage    │
                 └───────────────────────┘
                             │
                             ▼
                      weighbridge.db
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
          ▼                  ▼                  ▼
      AuditLogs          WeightLogs          Backups
          │                                      ▲
          │                                      │
          ▼                              backup_manager.py


                 ┌───────────────────────┐
                 │    sync_manager.py    │
                 └───────────────────────┘
                             │
                 ┌───────────┴───────────┐
                 │                       │
                 ▼                       ▼
            POST API                GET API
          Bulk Synchronization    Pending Trucks "perform in second -V"
                 │                       │
                 └───────────┬───────────┘
                             ▼
                       Remote Server
                           / ERP
```
---

# Project Structure


Weighbridge-Management-System/
│
├── main.py
├── database.py
├── weighbridge.py
├── script.py
├── sync_manager.py
├── backup_manager.py
├── requirements.txt
├── README.md
├── .gitignore
│
├── orbitserver/
│   ├── server.py
│   ├── requirements.txt
│   ├── README_SYNC.txt
│   └── start_server.bat
│
└── __pycache__/


## File Responsibilities

## `main.py`

This is the main entry point of the application.
It is responsible for:

* Starting the PySide6 application
* Initializing the local database
* Loading and saving application settings
* Managing the main application window
* Displaying the dashboard
* Managing truck Entry and Exit workflows
* Displaying truck records
* Managing application settings
* Connecting the live weighbridge service
* Integrating synchronization and backup services

The application interface includes major components such as:

* Dashboard
* Records Screen
* Settings Screen
* Main Application Window

---

## database.py

This file manages the complete local SQLite database layer.

The database and all run time files are created at:

```text
C:\Users\<User>\AppData\Local\Weighbridge
```

### Main responsibilities

* Initialize the database
* Create required tables
* Handle database migrations
* Create truck Entry records
* Complete truck Exit records
* Prevent duplicate active trucks
* Retrieve truck records
* Filter and search records
* Manage Entry and Exit relationships
* Track synchronization status
* Fetch unsynced records
* Mark records as synced or failed
* Manage audit logs
* Manage weight logs

### Entry and Exit Design

The system uses separate transactions for Entry and Exit.

```text
Truck Entry
     │
     ▼
Entry Record Created
     │
     │
Truck Exit
     │
     ▼
Separate Exit Record Created
     │
     ▼
Linked with Original Entry Record
```

This approach provides better tracking, synchronization, history management, and support for multi-gate or remote Exit workflows.

---

## `weighbridge.py`

This file handles real-time communication with the physical weighbridge machine.

### Responsibilities

* Detect available COM ports
* Connect to the configured serial port
* Continuously read weight data
* Parse incoming serial packets
* Extract numeric weight values
* Send live weight updates to the application
* Handle connection errors
* Reconnect when communication is interrupted

Example supported weight packet:

```text
ST,GS,+0001250.2,kg
```

The system can extract the actual weight value from the incoming serial data and display it in the application.

---

## `script.py`

This file contains the advanced serial communication service.

The main component is:

```text
WeighbridgeSerialService
```

Its responsibilities include:

* Connecting with the physical weighbridge
* Continuously reading serial data
* Extracting numeric weight values
* Maintaining multiple recent readings
* Detecting stable weight
* Handling connection failures
* Automatically reconnecting to the weighbridge

### Weight Processing Flow

```text
Raw Serial Data
        │
        ▼
Weight Parsing
        │
        ▼
Multiple Readings
        │
        ▼
Weight Stability Check
        │
        ▼
Stable Weight
        │
        ▼
Application
```

---

## `sync_manager.py`

This file manages communication between the local application and the ERP Server.

### Features

* Fetch unsynced records from SQLite
* Build API payloads
* Perform bulk synchronization
* Check internet availability
* Check server availability
* Track synchronization status
* Handle synchronization failures
* Run API operations in background threads

### Bulk Synchronization

Instead of sending an API request for every individual record, the system supports bulk synchronization.

```text
Local SQLite Database
        │
        ▼
Fetch Unsynced Records
        │
        ▼
Build JSON Payload
        │
        ▼
Single Bulk POST Request
        │
        ▼
Remote Server Response
        │
        ├──────────────► Successful Records → Mark Synced
        │
        └──────────────► Failed Records → Keep Unsynced
```

This approach reduces unnecessary API calls and improves synchronization efficiency.

---

## POST API — Bulk Sync

The POST API is used to synchronize local records with the ERP server.

The API configuration can be changed through the application settings.

Typical configuration includes:

* POST API URL
* Authentication Token
* Company ID
* Machine Number
* Other server-related configuration

---

## `backup_manager.py`

This file manages automatic backups of the local SQLite database.

The backup location is:

```text
C:\Users\<User>\AppData\Local\Weighbridge\backups\
```

### Backup Features

* Automatic database backup
* Daily backup scheduling
* Timestamped backup files
* SQLite-safe backup process
* Background backup operation
* Automatic cleanup of old backups

The backup system keeps a limited number of old backups to prevent unlimited disk usage.

---

## Application Data Storage

The application does not store its runtime data inside the Program Files directory.

Instead, all application data is stored in:

```text
C:\Users\<User>\AppData\Local\Weighbridge\
```

The directory structure is:

```text
Weighbridge/
│
├── AuditLogs/
│
├── backups/
│
├── WeightLogs/
│
├── settings.json
│
└── weighbridge.db
```

This approach avoids Windows permission problems and keeps application data separate from the application installation files.

---

## Audit Logs

Audit logs are stored in:

```text
C:\Users\<User>\AppData\Local\Weighbridge\AuditLogs\
```

The system creates daily log files.

Example:

```text
2026-08-25.log
2026-08-26.log
2026-08-27.log
```

The audit system records important application events and errors.

Examples include:

* Application startup
* Database initialization
* Truck Entry creation
* Truck Exit completion
* Synchronization success
* Synchronization failure
* API errors
* Serial communication errors
* System exceptions

This provides a complete event trail for troubleshooting and auditing.

---

## Live Weight Logs

Live weight data is stored in:

```text
C:\Users\<User>\AppData\Local\Weighbridge\WeightLogs\
```

Weight readings are periodically recorded to maintain a history of live weighbridge activity.

The logs can be useful for:

* Monitoring weighbridge activity
* Troubleshooting weight fluctuations
* Investigating hardware communication issues
* Maintaining historical weight data

---

## Application Settings

Application configuration is stored in:

```text
C:\Users\<User>\AppData\Local\Weighbridge\settings.json
```

The settings file can contain configuration such as:

```text
POST API URL
Authentication Token
Company Information
Machine Number
COM Port
Baud Rate
Serial Communication Settings
Other Application Configuration
```

The settings are dynamically loaded by the application.

This means API URLs and communication settings can be changed without modifying the source code.

---

## Local Database

The primary application database is:

```text
weighbridge.db
```

Location:

```text
C:\Users\<User>\AppData\Local\Weighbridge\weighbridge.db
```

The SQLite database stores the actual application data, including:

* Truck Entry records
* Truck Exit records
* Truck information
* Weight data
* Reference IDs
* Synchronization status
* Record timestamps

The application follows an **offline-first architecture**.

```text
Internet Available?
       │
       ├── YES ──► Sync with Server
       │
       └── NO
             │
             ▼
       Save Locally in SQLite
             │
             ▼
       Mark as Unsynced
             │
             ▼
       Sync Later When Available
```

---

## `orbitserver/`

The `orbitserver` directory contains a separate Flask-based test server.

It is primarily used for development and testing of synchronization functionality, you can  run and start json server and test api other wise use real Server.

## Main Files

### `server.py`

Provides API endpoints for:

* Health check
* Server information
* Request logging
* Bulk synchronization
* Record retrieval
* Single record retrieval
* Add record
* Update record
* Delete record

### `db.json`

Used as simple storage for the test server.

### `README_SYNC.txt`

Contains instructions for running and configuring the synchronization test server.

### `start_server.bat`

Starts the Flask test server on Windows.

---

## API Authentication

The synchronization system supports token-based authentication.

The authentication token is configured through the application settings instead of being permanently hardcoded into the application.

For security reasons, production API tokens should not be uploaded to a public GitHub repository.

---

## Installation

## 1. Clone the Repository

```bash
git clone <YOUR_REPOSITORY_URL>
cd Weighbridge-Management-System
```

## 2. Create a Virtual Environment

```bash
python -m venv venv
```

### Windows

```bash
venv\Scripts\activate
```

### Linux / macOS

```bash
source venv/bin/activate
```

## 3. Install Dependencies

```bash
pip install -r requirements.txt
```

## 4. Run the Application

```bash
python main.py
```

---

## Running the Test Sync Server

Navigate to:

```text
orbitserver/
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Then run:

```bash
python server.py
```

Or on Windows:

```text
start_server.bat
```

The test server can be used to verify the application's POST synchronization functionality before connecting to the production ERP server.

---

## Truck Workflow

## Truck Entry

```text
Truck Arrives
      │
      ▼
Weighbridge Reads Live Weight
      │
      ▼
Operator Enters Truck Information
      │
      ▼
Entry Record Created
      │
      ▼
Saved in Local SQLite Database
      │
      ▼
Audit Event Recorded
```

---

## Truck Exit

```text
Truck Ready for Exit
      │
      ▼
Pending Truck Retrieved
      │
      ▼
Live Exit Weight Read
      │
      ▼
Exit Transaction Created
      │
      ▼
Linked with Entry Reference
      │
      ▼
Stored in SQLite
      │
      ▼
Ready for Server Synchronization
```

---

## Synchronization Workflow

```text
Truck Entry / Exit Completed
              │
              ▼
       Store in SQLite
              │
              ▼
       Sync Status = Unsynced
              │
              ▼
       Internet Available?
              │
       ┌──────┴──────┐
       │             │
      NO            YES
       │             │
       ▼             ▼
Keep Local      Build Bulk Payload
       │             │
       │             ▼
       │       POST API Request
       │             │
       │             ▼
       │       Server Response
       │             │
       └─────────────┼─────────────┐
                     │             │
                     ▼             ▼
                Success          Failed
                     │             │
                     ▼             ▼
                 Mark Synced    Keep Unsynced
```

---

## Requirements

The project uses Python and the following major technologies:

* Python
* PySide6
* PySerial
* SQLite
* Flask for json server 
* REST APIs

Install the required dependencies using:

```bash
pip install -r requirements.txt
```

---

## Files Excluded from GitHub

The following files and directories should not be committed to the repository:

```text
__pycache__/
*.pyc

weighbridge.db
settings.json

AuditLogs/
WeightLogs/
backups/

*.log

orbitserver/db.json

.vscode/
.idea/

venv/
.venv/
```

---

## Security

Sensitive information should never be committed to a public repository.

Do not upload:

* Production API tokens
* Production server URLs
* Real customer data
* SQLite production database
* Audit logs
* Weight logs
* Backup files

Instead, create an example configuration file such as:

```text
settings.example.json
```

Example: for local json server

```json
{
    "sync_server_url": "https://your-server.com/api/sync",
    "pending_trucks_api": "https://your-server.com/api/pending-trucks",
    "token": "YOUR_TOKEN_HERE",
    "company_id": "",
    "machine_number": "",
    "com_port": "",
    "baud_rate": 9600
}
```

---

## Project Highlights

* **Offline-First:** The system continues working even when the internet or remote server is unavailable.
* **Real-Time Integration:** Direct communication with a physical weighbridge through serial/COM ports.
* **Separate Transactions:** Entry and Exit are handled as independent but linked transactions.
* **Dynamic Configuration:** API URLs, authentication, COM ports, and application settings are configurable.
* **Bulk Synchronization:** Multiple unsynced records can be synchronized efficiently.
* **Automatic Backup:** SQLite database backups are automatically maintained.
* **Audit Trail:** Important system events and errors are recorded in daily audit logs.
* **Live Weight History:** Periodic weight readings are stored for monitoring and troubleshooting.
* **Scalable Design:** The architecture supports remote Exit and multi-gate workflows.
* **Windows-Friendly Storage:** Runtime data is stored in the user's AppData directory instead of Program Files.

---

## Future Improvements

Possible future enhancements include:

* User authentication and role-based access
* Advanced reporting
* PDF receipt generation
* Cloud backup integration
* Multi-weighbridge support
* Centralized monitoring dashboard
* Real-time notifications
* Enhanced analytics and reporting
* Docker deployment for the server
* Production ERP integration
* Advanced retry mechanisms for failed synchronization

---

## 👨‍💻 Author
**Ahmed Bilal**
**Syed Raheel Ahmed Shah**
**Adnan Majeed**

Computer Science Student | Python | Software Development

---

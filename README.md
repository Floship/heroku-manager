# Heroku Manager

A Python package for managing Heroku dynos with autoscaling capabilities.

## Features

- Automatic scaling of Heroku dynos based on memory usage and load
- Monitoring of dyno health and performance
- Automatic restart of unresponsive dynos
- Integration with Django for caching and configuration
- Automatic cleaning of old files in specified directories via Heroku exec

## Installation

```bash
pip install heroku-manager
```

## Usage

### Basic Autoscaling

```python
from heroku_manager import HerokuManager

# Get the autoscaler instance
autoscaler = HerokuManager.get_autoscaler()

# Start continuous autoscaling
autoscaler.start_continuous_autoscale()

# Manual scaling operations
if autoscaler.requires_upscale:
    autoscaler.upscale_formation_to_next_level()
elif autoscaler.allow_downscale:
    autoscaler.downscale_formation_to_original_size()

# Stop autoscaling
autoscaler.stop_continuous_autoscale()
```

### File Cleaning

The package can automatically clean old files in specified directories on Heroku dynos. This is useful for preventing disk space issues.

To enable automatic file cleaning, set the following in your Django settings:

```python
# Enable file cleaning
DYNO_FILE_CLEANING_ENABLED = True

# Clean files older than 48 hours (default)
DYNO_FILE_AGE_THRESHOLD = 48

# Clean files every 24 hours (default)
DYNO_FILE_CLEANING_INTERVAL = 24 * 60 * 60

# Directory to clean (default: /tmp)
DYNO_FILE_CLEANING_DIRECTORY = '/tmp'
```

You can also manually clean files:

```python
from heroku_manager import HerokuManager

# Get the autoscaler instance
autoscaler = HerokuManager.get_autoscaler()

# Clean files in /tmp older than 48 hours
autoscaler.clean_old_files(directory='/tmp', hours=48)
```

## Configuration

The package requires the following environment variables:

- `HEROKU_API_KEY`: Your Heroku API key
- `HEROKU_APP_NAME`: The name of your Heroku app
- `DYNO`: The name of the current dyno (automatically set by Heroku)

Additionally, you must enable Heroku runtime metrics for proper memory monitoring:

```bash
heroku labs:enable log-runtime-metrics -a {app_name}
```

Without this, the `current_memory` feature will not work correctly.

## Django Settings

When used with Django, the following settings are available:

### Autoscaling Settings
- `DYNO_CONTINUOS_AUTOSCALE_ENABLED`: Enable continuous autoscaling
- `DYNO_AUTOSCALE_INTERVAL`: Interval between autoscale checks (in seconds)
- `DYNO_TIME_BETWEEN_SCALES`: Minimum time between scaling operations (in seconds)
- `DYNO_MIN_UPSCALE_DURATION`: Minimum duration to keep a dyno upscaled (in seconds)
- `DYNO_DOWNSCALE_CHECK_INTERVAL`: Interval for checking if a dyno can be downscaled (in seconds)
- `DYNO_ZOMBIE_THRESHOLD`: Time threshold for considering a dyno as unresponsive (in seconds)
- `DYNO_LOG_THREADS_USED`: Whether to log the number of threads used
- `DYNO_LOGS_CACHE_DURATION`: Duration to cache dyno logs (in seconds)
- `DYNO_ERRORS_TIMEOUT_DURATION`: Duration to cache error information (in seconds)
- `DYNO_GENERAL_CACHE_DURATION`: General cache duration (in seconds)
- `DYNO_TIME_BETWEEN_RESTARTS`: Minimum time between dyno restarts (in seconds)
- `DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER`: Whether to enable autoscaling for beat workers
- `UPSCALE_PERCENTAGE_HIGH_MEM_USE`: Memory usage percentage threshold for upscaling
- `DOWNSCALE_PERCENTAGE_HIGH_MEM_USE`: Memory usage percentage threshold for downscaling
- `HIGH_MEM_USE_MB`: High memory usage threshold in MB

### File Cleaning Settings
- `DYNO_FILE_CLEANING_ENABLED`: Enable automatic cleaning of old files (default: False)
- `DYNO_FILE_CLEANING_INTERVAL`: Interval between file cleaning operations in seconds (default: 24 hours)
- `DYNO_FILE_AGE_THRESHOLD`: Age threshold for files to be deleted in hours (default: 48 hours)
- `DYNO_FILE_CLEANING_DIRECTORY`: Directory to clean (default: /tmp)

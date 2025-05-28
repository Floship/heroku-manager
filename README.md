# Heroku Manager

A Python package for managing Heroku dynos with autoscaling capabilities.

## Features

- Automatic scaling of Heroku dynos based on memory usage and load
- Monitoring of dyno health and performance
- Automatic restart of unresponsive dynos
- Integration with Django for caching and configuration

## Installation

```bash
pip install heroku-manager
```

## Usage

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

## Configuration

The package requires the following environment variables:

- `HEROKU_API_KEY`: Your Heroku API key
- `HEROKU_APP_NAME`: The name of your Heroku app
- `DYNO`: The name of the current dyno (automatically set by Heroku)

## Django Settings

When used with Django, the following settings are available:

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

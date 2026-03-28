#!/usr/bin/env python3
"""
Website Monitor Module
Monitors websites and sends notifications if HTTP status is not 200
"""

import requests
import time
from typing import Optional, Callable, Dict, List
from datetime import datetime
import json


class WebsiteMonitor:
    """
    A class to monitor website health and send notifications on issues.
    """
    
    def __init__(self, timeout: int = 10, interval: int = 300):
        """
        Initialize the Website Monitor.
        
        Args:
            timeout: Request timeout in seconds (default: 10)
            interval: Check interval in seconds (default: 300 = 5 minutes)
        """
        self.timeout = timeout
        self.interval = interval
        self.notification_callback: Optional[Callable] = None
        self.monitored_sites: List[Dict] = []
        self.last_status: Dict[str, int] = {}  # site_url -> last_http_code
    
    def set_notification_callback(self, callback: Callable):
        """
        Set the notification callback function.
        
        Args:
            callback: Function to call when issues are detected.
                     Should accept parameters: (site_url, status_code, timestamp)
        """
        self.notification_callback = callback
    
    def monitor_site(self, url: str, site_name: Optional[str] = None) -> Dict:
        """
        Monitor a single website and check its HTTP status.
        
        Args:
            url: The website URL to monitor
            site_name: Optional name for the site (defaults to URL)
            
        Returns:
            Dict containing the monitoring result
        """
        if site_name is None:
            site_name = url
            
        result = {
            'site_name': site_name,
            'url': url,
            'status': 'success',
            'status_code': 200,
            'response_time': 0,
            'message': '',
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            start_time = time.time()
            response = requests.get(url, timeout=self.timeout)
            elapsed_time = time.time() - start_time
            response_time = round(elapsed_time, 2)
            
            result['response_time'] = response_time
            
            if response.status_code == 200:
                result['status'] = 'success'
                result['message'] = 'Website is up and running'
                self.last_status[url] = 200
            else:
                result['status'] = 'error'
                result['status_code'] = response.status_code
                result['message'] = self._generate_error_message(response.status_code)
                self.last_status[url] = response.status_code
                
                if self.notification_callback:
                    self.notification_callback(url, response.status_code, result)
                    
        except requests.exceptions.RequestException as e:
            result['status'] = 'error'
            result['status_code'] = 0
            result['message'] = f"Connection failed: {str(e)}"
            self.last_status[url] = 0
            if self.notification_callback:
                self.notification_callback(url, 0, result)
        
        self.monitored_sites.append({
            'site_name': site_name,
            'url': url,
            'timestamp': datetime.now().isoformat(),
            'last_status': result['status'],
            'last_code': result['status_code']
        })
        
        return result
    
    def _generate_error_message(self, status_code: int) -> str:
        """
        Generate a human-readable error message for a status code.
        
        Args:
            status_code: The HTTP status code
            
        Returns:
            Error message string
        """
        messages = {
            301: "Redirect detected - site may have moved permanently",
            302: "Temporary redirect detected",
            304: "Not Modified - content unchanged",
            400: "Bad Request - server cannot process the request",
            401: "Unauthorized - authentication required",
            403: "Forbidden - access denied to the resource",
            404: "Not Found - page/resource does not exist",
            405: "Method Not Allowed - HTTP method not supported",
            408: "Request Timeout - server timed out waiting for request",
            410: "Gone - resource permanently removed",
            429: "Too Many Requests - rate limit exceeded",
            500: "Internal Server Error - server error",
            501: "Not Implemented - server doesn't support functionality",
            502: "Bad Gateway - invalid response from upstream",
            503: "Service Unavailable - server overloaded or down",
            504: "Gateway Timeout - upstream server timeout",
        }
        return messages.get(status_code, f"HTTP error: {status_code}")
    
    def monitor_multiple_sites(self, sites: List[Dict]) -> Dict:
        """
        Monitor multiple websites at once.
        
        Args:
            sites: List of dicts with 'url' and optionally 'site_name' keys
            
        Returns:
            Dict containing results for all sites
        """
        results = {}
        for site_info in sites:
            url = site_info['url']
            site_name = site_info.get('site_name', url)
            results[site_name] = self.monitor_site(url, site_name)
        
        return results
    
    def run_continuous_monitoring(self, sites: List[Dict], callback: Optional[Callable] = None) -> None:
        """
        Run continuous monitoring of sites with specified interval.
        
        Args:
            sites: List of site dicts to monitor
            callback: Optional callback function for each check
        """
        if callback:
            self.notification_callback = callback
            
        while True:
            self.monitor_multiple_sites(sites)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Monitoring cycle completed")
            time.sleep(self.interval)
    
    def check_all_sites(self, sites: List[Dict]) -> Dict:
        """
        Check all monitored sites and return status summary.
        
        Args:
            sites: List of site dicts
            
        Returns:
            Summary dict with overall health status
        """
        results = self.monitor_multiple_sites(sites)
        
        summary = {
            'total_sites': len(sites),
            'healthy_sites': 0,
            'unhealthy_sites': 0,
            'details': results
        }
        
        for result in results.values():
            if result['status'] == 'success':
                summary['healthy_sites'] += 1
            else:
                summary['unhealthy_sites'] += 1
        
        return summary


def notify_email(url: str, status_code: int, result: Dict):
    """
    Example callback function to send email notification.
    """
    print(f"\n⚠️  ALERT: {result['site_name']} ({url})")
    print(f"   Status: {result['status'].upper()}")
    print(f"   HTTP Code: {status_code}")
    print(f"   Message: {result['message']}")
    print(f"   Time: {result['timestamp']}")
    print("-" * 50)


def notify_slack(url: str, status_code: int, result: Dict):
    """
    Example callback function to send Slack notification.
    """
    # In real implementation, replace with webhooks or API calls
    print(f"🚨 SLACK ALERT: {result['site_name']} down (Code: {status_code})")
    print(f"   URL: {url}")
    print(f"   Time: {result['timestamp']}")
    print("-" * 30)


def notify_webhook(url: str, status_code: int, result: Dict, webhook_url: str):
    """
    Example callback function to send webhook notification.
    """
    import urllib.request
    import urllib.parse
    
    data = json.dumps({
        'site': result['site_name'],
        'url': url,
        'status': result['status'],
        'status_code': status_code,
        'message': result['message'],
        'timestamp': result['timestamp']
    }).encode('utf-8')
    
    req = urllib.request.Request(webhook_url, data=data)
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'WebsiteMonitor/1.0')
    
    try:
        response = urllib.request.urlopen(req, timeout=10)
        print(f"✓ Notification sent to webhook")
    except Exception as e:
        print(f"✗ Failed to send webhook notification: {e}")


def main():
    """
    Example usage of the Website Monitor.
    """
    # Initialize monitor
    monitor = WebsiteMonitor(
        timeout=10,
        interval=300  # Check every 5 minutes
    )
    
    # Define sites to monitor
    sites = [
        {'url': 'https://www.google.com', 'site_name': 'Google'},
        {'url': 'https://www.github.com', 'site_name': 'GitHub'},
        {'url': 'https://www.example.com', 'site_name': 'Example'},
        {'url': 'https://httpbin.org/status/404', 'site_name': '404 Test Site'},
    ]
    
    # Set notification callback (using email example)
    monitor.set_notification_callback(lambda url, status, result: notify_email(url, status, result))
    
    # Check all sites once
    print("Checking sites...")
    summary = monitor.check_all_sites(sites)
    
    print(f"\n=== Monitoring Summary ===")
    print(f"Total Sites: {summary['total_sites']}")
    print(f"Healthy: {summary['healthy_sites']}")
    print(f"Unhealthy: {summary['unhealthy_sites']}")
    
    for site_name, result in summary['details'].items():
        status_icon = "✅" if result['status'] == 'success' else "❌"
        print(f"{status_icon} {site_name}: HTTP {result['status_code']} - {result['message']}")


if __name__ == "__main__":
    main()
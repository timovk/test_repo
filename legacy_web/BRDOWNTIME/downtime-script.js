// Countdown Timer Function
function updateCountdown(endDate) {
    const countdown = document.getElementById('countdown');
    const daysElement = document.getElementById('days');
    const hoursElement = document.getElementById('hours');
    const minutesElement = document.getElementById('minutes');
    const secondsElement = document.getElementById('seconds');
    const messageElement = document.querySelector('.timer-status-container');
    const enterButton = document.getElementById('enterButton'); // "Enter Website" button

    function calculateTimeRemaining() {
        const currentDate = new Date();
        const timeRemaining = endDate - currentDate;

        // Calculate days, hours, minutes, and seconds
        const days = Math.floor(timeRemaining / (1000 * 60 * 60 * 24));
        const hours = Math.floor((timeRemaining % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
        const minutes = Math.floor((timeRemaining % (1000 * 60 * 60)) / (1000 * 60));
        const seconds = Math.floor((timeRemaining % (1000 * 60)) / 1000);

        return {
            days: String(days).padStart(2, '0'),
            hours: String(hours).padStart(2, '0'),
            minutes: String(minutes).padStart(2, '0'),
            seconds: String(seconds).padStart(2, '0'),
            timeRemaining
        };
    }

    function updateDisplay() {
        const { days, hours, minutes, seconds, timeRemaining } = calculateTimeRemaining();
        if (timeRemaining > 0) {
            // Update the countdown numbers
            daysElement.textContent = days;
            hoursElement.textContent = hours;
            minutesElement.textContent = minutes;
            secondsElement.textContent = seconds;
        } else {
            // When countdown ends, hide countdown and show message
            countdown.style.display = 'none';
            
            // Show the message
            messageElement.textContent = 'Fortnite: Super is out now!';
            messageElement.style.fontSize = '40px';
            messageElement.style.marginBottom = '20px'; // Ensure space between message and button

            // Make the "Enter Website" button more prominent after the countdown ends
            enterButton.style.fontSize = '20px';

            clearInterval(timerInterval); // Stop the countdown updates
        }
    }

    updateDisplay();
    const timerInterval = setInterval(updateDisplay, 1000);
}

// Example end time for the countdown
const endTime = new Date("January 17, 2025 15:00:00").getTime();
updateCountdown(endTime);

// Scroll-to-Top Button Functionality
const scrollToTopBtn = document.getElementById('scrollToTopBtn');

// Show the button when scrolled down 100px
window.addEventListener('scroll', () => {
    if (document.body.scrollTop > 100 || document.documentElement.scrollTop > 100) {
        scrollToTopBtn.style.display = "block";
    } else {
        scrollToTopBtn.style.display = "none";
    }
});

// Smooth scroll to top when button is clicked
scrollToTopBtn.addEventListener('click', () => {
    window.scrollTo({
        top: 0,
        behavior: 'smooth'
    });
});

// News Ticker functionality (no refresh behavior)
document.addEventListener("DOMContentLoaded", function() {
    const ticker = document.querySelector('.news-ticker');
    const tickerContent = document.querySelectorAll('.ticker-content span');
    let tickerPreviouslyEmpty = true; // Track if the ticker was empty previously

    function checkForUpdates() {
        const currentDate = new Date();
        let hasVisibleItems = false;

        tickerContent.forEach(item => {
            const itemDateAttr = item.getAttribute('data-date');
            const itemDate = new Date(itemDateAttr);
            if (isNaN(itemDate)) {
                console.error(`Invalid date format for data-date: "${itemDateAttr}" in element`, item);
                item.style.display = 'none';
                return;
            }

            const duration = parseFloat(item.getAttribute('data-duration')) || 8; // Default to 8 hours
            const endDate = new Date(itemDate.getTime() + duration * 60 * 60 * 1000);

            if (currentDate >= itemDate && currentDate <= endDate) {
                item.style.display = 'inline';
                hasVisibleItems = true; // Found a visible item
            } else {
                item.style.display = 'none';
            }
        });

        // Handle ticker visibility
        if (!hasVisibleItems) {
            ticker.style.display = 'none';
        } else {
            ticker.style.display = 'block';
        }

        return hasVisibleItems;
    }

    function startUpdateCheck() {
        setInterval(() => {
            const hasVisibleItems = checkForUpdates();

            // Simply check for visibility without refreshing
            tickerPreviouslyEmpty = !hasVisibleItems;
        }, 1000); // Check every 60 seconds
    }

    // Initial check
    checkForUpdates();
    
    // Start periodic checking for updates
    startUpdateCheck();
});

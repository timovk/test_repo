var elapsedPercentage;

function addLeadingZero(number) {
    return (number < 10 ? '0' : '') + number;
}

function roundDownToTwoDecimals(number) {
    return Math.floor(number * 100) / 100;

}

function updateProgressBar() {
    var progressBar = document.getElementById("progressBar");
    var progressLabel = document.getElementById("progressLabel");
    var startDate = new Date("June 8, 2025, 00:10:00");
    var endDate = new Date("August 8, 2025, 08:30:00");
    var currentDate = new Date();
    var totalDuration = endDate - startDate;
    var elapsedDuration = currentDate - startDate;
    //Standard season code below:
    elapsedPercentage = (elapsedDuration / totalDuration) * 100;
    //Code for new season downtime below:
    //elapsedPercentage = 0;
    progressBar.value = elapsedPercentage;
    progressLabel.textContent = roundDownToTwoDecimals(elapsedPercentage) + "%";
    if (elapsedPercentage >= 100) {
        progressBar.value = 100;
        progressLabel.textContent = "100%";
    }
}

updateProgressBar();

setInterval(updateProgressBar, 1);

function updateCountdown() {
    var countdownElement = document.getElementById("countdown");
    var endDate = new Date("August 8, 2025, 08:30:00");
    var currentDate = new Date();
    var timeDifference = endDate - currentDate;
    if (timeDifference <= 0) {
        countdownElement.textContent = "Super has concluded.";
        countdownElement.innerHTML = "Downtime is currently active for the release of C6S4. Stay tuned!"
    } else {
        var days = Math.floor(timeDifference / (1000 * 60 * 60 * 24));
        var hours = Math.floor((timeDifference % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
        var minutes = Math.floor((timeDifference % (1000 * 60 * 60)) / (1000 * 60));
        var seconds = Math.floor((timeDifference % (1000 * 60)) / 1000);

        days = addLeadingZero(days);
        hours = addLeadingZero(hours);
        minutes = addLeadingZero(minutes);
        seconds = addLeadingZero(seconds);

        countdownElement.innerHTML = "Super will end in:<br> " + '<span style="font-size:100px;">' + days + ":" + hours + ":" + minutes + ":" + seconds + '</span>';
        
        
    }
}

setInterval(updateCountdown, 1);

updateCountdown()



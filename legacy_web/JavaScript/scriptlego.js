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
    
    
    //When event pass is active, active below code line:
    elapsedPercentage = (elapsedDuration / totalDuration) * 100;
    
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


    //When event pass is active, use below code:
    if (timeDifference <= 0) {
        countdownElement.textContent = "Supernova Academy has concluded.";
    
    //Else, use this:
    /*if (timeDifference <= 0) {
        countdownElement.textContent = "Stay tuned for a new LEGO Pass!";*/
        
        } else {
        var days = Math.floor(timeDifference / (1000 * 60 * 60 * 24));
        var hours = Math.floor((timeDifference % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
        var minutes = Math.floor((timeDifference % (1000 * 60 * 60)) / (1000 * 60));
        var seconds = Math.floor((timeDifference % (1000 * 60)) / 1000);

        days = addLeadingZero(days);
        hours = addLeadingZero(hours);
        minutes = addLeadingZero(minutes);
        seconds = addLeadingZero(seconds);

        //When event pass is active, use below code line:
        countdownElement.innerHTML = "Supernova Academy will end in:<br> " + '<span style="font-size: 100px; font-weight: bold;">' + days + ":" + hours + ":" + minutes + ":" + seconds + '</span>';
    }
}

setInterval(updateCountdown, 1);

updateCountdown()
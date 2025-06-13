window.lastUsername = null;
{% if Settings._CREATE_MEMBERS in session.settings %}
const addUserButton = document.querySelector("#addUserForm");
const addUserUsernameInput = document.querySelector("#addUserForm #username");
const addUserPgroupInput = document.querySelector("#addUserForm #pgroup");
const addUserSettingsDiv = document.querySelector("#addUserForm div.settings-box");
const addUserMessageBox = document.querySelector("#addUserForm .message");
const addUserMessageBoxText = document.querySelector("#addUserForm .message p");
const addUserMessageBoxLinkElm = document.querySelector("#addUserForm .message .copy-link");
window.addUserMessageBoxLink = "";
function copyAddUserMessageLink(event) {
    navigator.clipboard.writeText(addUserMessageBoxLink);
}
function addUserSuccess(data) {
    const success = data.success;
    if (success) {
        addUserMessageBoxText.textContent = lastUsername+" was added.";
        addUserMessageBoxLinkElm.style.display = "";
        window.addUserMessageBoxLink = "https://"+location.host+"{{ url_for('register') }}?user_slot="+data.{{ consts.FIELD_DATA }};
    }
    else {
        switch (data.reason) {
            case "{{ exceptions.AlreadyExistsError.identifier }}":
                addUserMessageBoxText.textContent = "Nutzername wird bereits verwendet.";
                break;
            case "{{ exceptions.UsernameTooLong.identifier }}":
                addUserMessageBoxText.textContent = "Nutzername muss kürzer sein.";
                break;
            case "{{ exceptions.UsernameTooShort.identifier }}":
                addUserMessageBoxText.textContent = "Nutzername muss länger sein.";
                break;
            case "{{ exceptions.CannotBeNamedAnonymous.identifier }}":
                addUserMessageBoxText.textContent = "Dein Nutzername kann nicht 'Anonymous' sein.";
                break;
            case "{{ exceptions.UsernameInvalidCharacters.identifier }}":
                addUserMessageBoxText.textContent = "Nutzername muss aus den Zeichen a-z, A-Z, 0-9, _ und - bestehen.";
                break;
            default:
                addUserMessageBoxText.textContent = "Konnte nicht erstellt werden.";
        }
    }
    addUserMessageBox.style.display = "";
}
addUserButton.addEventListener("submit", (event) => {
    addUserMessageBoxText.textContent = "...";
    event.preventDefault();
    window.lastUsername = addUserUsernameInput.value;
    let settingsValue = 0;
    for (const child of addUserSettingsDiv.children) {
        settingsValue |= settingsValues[child.children[0].name] * child.children[0].checked;
    }
    console.log(settingsValue);
    fetch("{{ url_for('add_user') }}", {
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            '{{ consts.FIELD_CSRF_TOKEN_HEADER }}': getCookie("{{ consts.FIELD_CSRF_TOKEN }}")
        },
        method: "POST",
        body: JSON.stringify({
            {{ consts.FIELD_USERNAME }}: lastUsername,
            {{ consts.FIELD_PERMISSION_GROUP }}: Number(addUserPgroupInput.value),
            {{ consts.FIELD_SETTINGS }}: settingsValue
        })
    }).then((response) => response.json()).then(addUserSuccess);
})
{% endif %}
The idea is to make a team of tools for a claude-agent-sdk client.

There is a main dev lab where the work is being made. I am thinking 
it can be something small like a raspberry pi 5. Goal is that it can
run uninterupted.

Code is managed through a git repo. Lets just try a github repo.

To this dev lab a set of clients can be connected.
 - Chat client. Can start and control the dev lab
 - Extension clients. Supply external features on other machines,
   ie being able to build and run test on a macOS, tho work was made
   on the raspberry pi 5.

Think main toolchain should be python and use venvs for simplicity.
